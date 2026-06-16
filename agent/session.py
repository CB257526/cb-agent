"""AgentSession：纯逻辑会话核心，无 print。

Stage 3 拆出来的"中间层"。它把 Stage 1+2 的 EventBus / ToolExecutor 跟原来
AgentRunner 的会话主流程组合起来，但**不直接做任何输出**——所有"现在发生了什么"
都经 EventBus 派发，留给前端（CLIRenderer / TextualApp / FastAPI）订阅渲染。

跟原 AgentRunner 的差别：
- _chat_once → chat()：返回 final_answer，让 REPL 决定怎么展示
- _tool_loop：继续在这里，但每轮 think 传 event_bus，工具循环的 RoundStart /
  RoundEnd / Error / Done 也都经 bus 而非 print
- _build_system_instructions / _prepend_background_notifications：纯字符串
  组装，跟 print 无关，原样搬过来
- 历史管理（self.history）也在 session 里（前端无需知道历史结构）

不在这里:
- 启动期 _section/_info：装配阶段的输出，仍由 run_agent.py 主入口打
- /xxx 斜杠命令：CLI 专属功能，REPL 那边处理
- 渲染逻辑（颜色 / 面板）：CLIRenderer 那边

上下文工程模块对接 (Claude Code 对齐重构):
- 旧 ContextBuilder/ContextPacket 已删除,改走 context.get_system_prompt
  组装 list[str] -> build_system_prompt_blocks -> provider_adapter.emit_system
  -> 单 string 进 messages[0]。
- memory_loader / provider_adapter 在 run_agent.py 装配,这里只持依赖。
- _build_chat_messages 不再使用 GSSC 流水线;state/compact/work_record 链路
  保留(用户决策: work_context.py 完整保留)。

ToolRegistry / Executor / LLM 仍从外部传入,便于测试和换前端。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from agent.cancel import (
    CancelToken,
    set_current_cancel_token,
    reset_current_cancel_token,
)
from agent.cb_agents import CbAgentsLLM
from agent.event_bus import EventBus
from agent.events import (
    BackgroundNotification, Cancelled, Done, Error, RoundEnd, RoundStart,
)
from agent.executor import ToolExecutor
from agent.message_logger import MessageLogger
from agent.multimodal_input import process_multimodal_prompt, sanitize_multimodal_payload
from agent.question_registry import QuestionRegistry
from constant.llm.constant_llm import ConstantLLM
from context import (
    MemoryLoader,
    OpenAICompatibleAdapter,
    build_system_prompt_blocks,
    clear_system_prompt_sections,
    compact_now,
    count_tokens,
    get_system_prompt,
    should_use_global_cache_scope,
)
from context.cache.provider_adapter import CacheControlAdapter
from core.message import Message
from skills.skill_manager import SkillManager
from tools.toolRegistry import ToolRegistry
from agent.work_context import (
    COMPACT_RECORD_LIMIT,
    LocalSessionStore,
    RuleTraceSummarizer,
    TraceCollector,
    TraceSummarizer,
    make_compact_record_message,
    make_work_record_message,
)
from agent.pet import PetManager

logger = logging.getLogger(__name__)


def _clip_compact_text(text: Any, limit: int = COMPACT_RECORD_LIMIT) -> str:
    """把 compact 相关文本裁到固定长度。

    /compact 的摘要会进入下一轮 prompt，也会落盘到 compact.json。这里不用
    work_context._clip 这个私有 helper，是为了让 session.py 的公共行为不依赖
    下划线函数；但语义保持一致：折叠空白、硬截断，避免摘要反过来撑爆上下文。
    """
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _message_content_to_text(content: Any) -> str:
    """把 Message.content 转成 TUI/RPC 可直接展示的短文本。

    ``core.message.Message`` 的 user content 可能是多模态数组，而 assistant content
    通常是字符串。这里不返回 OpenAI 原始 message dict，是为了避免把 tool_calls、
    tool_call_id 等本轮协议字段暴露给跨会话恢复流程；前端只需要普通可视文本。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = item.get("type")
            if item_type == "text":
                parts.append(str(item.get("text") or ""))
            elif item_type == "image_url":
                url = (item.get("image_url") or {}).get("url", "")
                parts.append(f"[image: {url}]" if url else "[image]")
            elif item_type == "audio_url":
                url = (item.get("audio_url") or {}).get("url", "")
                parts.append(f"[audio: {url}]" if url else "[audio]")
        return " ".join(p for p in parts if p).strip()
    return str(content)


def _history_message_to_payload(message: Message) -> Dict[str, Any]:
    """把内存 history 消息转成 JSON-RPC/TUI 恢复用的轻量结构。

    这里故意只输出 role/content/kind 三个字段。跨会话切换恢复的是"用户看到的
    对话记录 + 工作记录文本"，不是工具调用协议，因此不带 tool role，也不带
    assistant.tool_calls。
    """
    role = message.role.value if hasattr(message.role, "value") else str(message.role)
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return {
        "role": role,
        "content": _message_content_to_text(message.content),
        "kind": metadata.get("kind"),
    }


def _message_role_name(message: Message) -> str:
    """返回 Message 的 role 字符串，兼容 Enum 和普通字符串两种形态。"""
    return message.role.value if hasattr(message.role, "value") else str(message.role)


def _message_kind(message: Message) -> str:
    """读取本地 message kind。

    work_record/compact_record 在 OpenAI 协议里都是普通 assistant message，
    本地只靠 metadata.kind 区分用途。/compact 保留最近一轮时要排除这类
    维护性消息，只留下真正的 user/assistant 对话。
    """
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("kind") or "")


class _SessionCompactSummarizer:
    """`context.compact.compact_now` 用于压缩 AgentSession 历史记录的适配器。"""

    def __init__(self, session: "AgentSession", *, state_text: str) -> None:
        self.session = session
        self.state_text = state_text

    async def summarize(
        self,
        messages: Sequence[Message],
        *,
        focus: Optional[str] = None,
    ) -> Optional[str]:
        """压缩历史记录，返回摘要。"""
        return self.session._make_compact_summary(
            messages=messages,
            state_text=self.state_text,
            focus=focus,
        )


def _context_message_line(message: Message) -> str:
    """把一条跨轮 history 渲染成上下文估算用的单行文本。

    这里不是给 UI 展示，也不是还原 OpenAI 原始 message；它只服务 token 估算。
    因此保留 role/kind/content 三类会影响 prompt 体积的信息即可，继续排除
    tool_call_id、tool_calls 等只属于单轮工具协议的字段。
    """
    role = _message_role_name(message)
    kind = _message_kind(message)
    content = _message_content_to_text(message.content)
    label = role if not kind else f"{role}/{kind}"
    return f"{label}: {content}".strip()


class AgentSession:
    """单个 agent 会话。一个进程里通常只有一个，但多会话场景也支持。

    构造时把所有依赖注入进来；运行时只暴露 chat() 一个入口。
    """

    # 工具调用循环最大轮数，防死循环
    MAX_TOOL_ROUNDS = 200
    # 当前工具循环的完整 tool result 被压缩进 messages 时，每条 tool message
    # 最多保留的字符数。它比跨轮 TraceCollector 的 100 字符略宽，是因为本轮
    # 模型还需要靠这条摘要继续推理；但仍然要有硬边界，防止 file_read/stdout
    # 这类结果把下一次 think 请求撑爆。
    AUTO_TOOL_MESSAGE_LIMIT = 700

    def __init__(
        self,
        llm: CbAgentsLLM,
        registry: ToolRegistry,
        executor: ToolExecutor,
        event_bus: EventBus,
        memory_loader: Optional[MemoryLoader] = None,
        provider_adapter: Optional[CacheControlAdapter] = None,
        skill_manager: Optional[SkillManager] = None,
        bash_prompt_provider=None,
        ctx_enabled: bool = True,
        history_window: int = 12,  # 最多保留 12 轮历史记录
        messages_snapshot_hook=None,
        session_store: Optional[LocalSessionStore] = None,
        trace_summarizer: Optional[TraceSummarizer] = None,
        message_logger: Optional[MessageLogger] = None,
        language: Optional[str] = "Chinese",
        mcp_clients=None,
        pet_manager: Optional[PetManager] = None,
    ) -> None:
        """
        Args:
            messages_snapshot_hook: 可选回调 (messages, round_idx) -> None,
                每轮 think 前调用一次。给 CLI dump 调试用,不属于事件流(事件
                是结构化的;dump 是面向开发者的"看原始上下文"调试通道)。
            message_logger: 可选消息日志记录器。非 None 时,在每次 LLM 调用前后
                将完整 messages 列表写入独立日志文件,包含所有 role 的消息全文。
            memory_loader: 多级 CLAUDE.md 加载器(对齐 Claude Code 的 claudemd.ts)。
                为 None 时 system prompt 不注入 memory section,适合 --bare 模式。
            provider_adapter: 把 SystemPromptBlock 列表转成具体 API 字段的适配器。
                默认 OpenAICompatibleAdapter(国内厂商兼容),join 成单 string。
        """
        self.llm = llm
        self.registry = registry
        self.executor = executor
        self.event_bus = event_bus
        self.memory_loader = memory_loader
        self.provider_adapter = provider_adapter or OpenAICompatibleAdapter()
        self.skill_manager = skill_manager
        self.bash_prompt_provider = bash_prompt_provider
        self.ctx_enabled = ctx_enabled
        self.history_window = history_window
        self.messages_snapshot_hook = messages_snapshot_hook
        self.session_store = session_store
        self.trace_summarizer = trace_summarizer
        self.message_logger = message_logger
        self.language = language
        self.mcp_clients = mcp_clients
        self.pet_manager = pet_manager
        self.rule_trace_summarizer = RuleTraceSummarizer()
        self.history: List[Message] = []
        if self.session_store is not None:
            try:
                # 启动自动恢复最近会话:这里只恢复普通 user/assistant 消息和
                # 【工作记录】assistant 消息,不恢复 role=tool,也不恢复
                # assistant.tool_calls。tool 协议消息只在同一轮 tool loop 内合法,
                # 跨进程/跨轮直接回灌会造成 tool_call_id 对不上的协议风险。
                self.history = self.session_store.load_latest_history(
                    max_messages=self.history_window,
                )
            except Exception:
                logger.exception("本地会话历史恢复失败,忽略")
                self.history = []
        # 当前正在跑的 chat 的 cancel token;REPL 收 Ctrl-C 时调它的 .cancel()
        # 没在 chat 中时为 None
        self.current_cancel_token: Optional[CancelToken] = None
        # AskUserQuestionTool 用:工具线程 register+wait,gateway 在 RPC 里
        # submit_answer。整个进程一份,session 持有给 gateway/tool 共享。
        self.question_registry: QuestionRegistry = QuestionRegistry()
        # MCP 后台加载由 AgentRunner 装配,但 Gateway 只持有 AgentSession。
        # 因此这里暴露两个可选回调槽位:
        # - mcp_status_provider:只读当前连接快照;
        # - mcp_background_loader:幂等启动后台连接并返回快照。
        # 这两个状态只服务 UI/CLI 展示,不写入 history,也不参与 system prompt。
        self.mcp_status_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.mcp_background_loader: Optional[Callable[[], Dict[str, Any]]] = None
        logger.info(
            "AgentSession initialized: ctx_enabled=%s history_window=%s restored_history=%s message_logger=%s",
            self.ctx_enabled,
            self.history_window,
            len(self.history),
            bool(self.message_logger),
        )

    # ---------- 公共入口 ----------

    def export_history(self) -> List[Dict[str, Any]]:
        """导出当前内存 history，供 RPC/TUI 在切换会话后重绘屏幕。

        这不是给 LLM 的上下文构造函数；LLM 仍然走 ``self.history`` +
        ContextBuilder。导出层只服务 UI，因此会丢弃协议字段并保留普通文本。
        """
        return [_history_message_to_payload(m) for m in self.history]

    def list_sessions(self) -> List[Dict[str, Any]]:
        """列出本地会话摘要；未启用 session_store 时返回空列表。"""
        if self.session_store is None:
            return []
        return self.session_store.list_sessions()

    def current_session_payload(self) -> Dict[str, Any]:
        """返回当前会话摘要和可渲染 history。

        Gateway 的 ready/切换响应、TUI 的会话面板都可以复用这个形状，避免前端
        自己猜测 active session 和 history 的对应关系。
        """
        summary = None
        if self.session_store is not None:
            summary = self.session_store.current_session_summary()
        return {
            "session": summary,
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
        }

    def create_session(self) -> Dict[str, Any]:
        """创建并切换到一个全新的空会话。

        新会话的隔离语义是：磁盘 active 指针切到新目录，同时内存 history 清空。
        后续 chat 会写入新目录，不会继续追加旧 transcript。
        """
        self.history.clear()
        if self.session_store is None:
            return {"session": None, "history": [], "context_window": self.context_window_usage()}
        summary = self.session_store.create_session()
        return {"session": summary, "history": [], "context_window": self.context_window_usage()}

    def switch_session(self, session_id: str) -> Dict[str, Any]:
        """切换到已有会话并恢复它最近的普通 history。

        这一步只读该 session 目录下的 transcript/state；不会把当前会话内容保存到
        目标会话，也不会生成新的 transcript 行。会话隔离边界完全由
        LocalSessionStore.switch_session 的目录校验保证。
        """
        if self.session_store is None:
            raise RuntimeError("local session store is not enabled")
        summary = self.session_store.switch_session(session_id)
        self.history = self.session_store.load_latest_history(
            max_messages=self.history_window,
        )
        return {
            "session": summary,
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
        }

    def compact_context(self) -> Dict[str, Any]:
        """压缩当前会话上下文，释放下一轮 prompt 的近轮历史占用。

        /compact 和 /clear 的语义不同：
        - /clear 是彻底删除当前 session 文件并清空内存；
        - /compact 保留 transcript 审计，只把“后续会注入模型的 history”
          压成一条 `【上下文压缩】` 摘要，并保留最近一轮普通对话。

        这里不 emit TextDelta/Done，也不调用 llm.think()。它是一个同步管理 RPC，
        TUI 只会收到 RPC response，然后追加系统提示。
        """
        before_messages = len(self.history)
        state_text = self._session_state_text()
        if before_messages == 0 and not state_text:
            return {
                "session": self.current_session_payload().get("session"),
                "history": self.export_history(),
                "context_window": self.context_window_usage(),
                "summary": "",
                "before_messages": 0,
                "after_messages": 0,
                "persisted": False,
                "no_op": True,
            }

        retained_turn = self._latest_plain_turn_messages()
        compact_source = list(self.history)
        if not compact_source and state_text:
            compact_source = [
                Message.create_user_message("[本地滚动状态]\n" + state_text)
            ]

        compact_result = asyncio.run(compact_now(
            compact_source,
            model=getattr(self.llm, "model", "") or "",
            summarizer=_SessionCompactSummarizer(self, state_text=state_text),
            session_state=None,
            memory_loader=self.memory_loader,
            keep_recent_messages=0,
        ))
        summary = compact_result.summary or self._rule_compact_summary(
            messages=compact_source,
            state_text=state_text,
        )
        compact_message = make_compact_record_message(summary)
        new_history = [compact_message] + retained_turn
        after_messages = len(new_history)

        # 压缩后的内存 history 是唯一会进入下一轮 ContextBuilder 的近轮历史。
        # compact_message 承担旧上下文摘要职责；retained_turn 保留用户刚刚说过的
        # 话和助手最终回答，避免 compact 后立刻丢掉最贴近当前任务的语气/细节。
        persisted = False
        if self.session_store is not None:
            try:
                self.session_store.save_compaction(
                    summary=str(compact_message.content or ""),
                    history_payload=[
                        _history_message_to_payload(message)
                        for message in new_history
                    ],
                    before_messages=before_messages,
                    after_messages=after_messages,
                )
                persisted = True
            except Exception:
                logger.exception("本地会话 compact 快照落盘失败")
                raise

        self.history = new_history

        return {
            "session": self.current_session_payload().get("session"),
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
            "summary": str(compact_message.content or ""),
            "before_messages": before_messages,
            "after_messages": after_messages,
            "persisted": persisted,
            "no_op": False,
        }

    def chat(
        self,
        user_query: str,
        cancel_token: Optional[CancelToken] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        persistent_user_text: Optional[str] = None,
    ) -> str:
        """处理一次用户输入，返回最终答案字符串。

        全程经 self.event_bus 派发事件，本方法不直接输出任何字符。

        Args:
            cancel_token: 可选取消令牌。调 .cancel() 后：
              - LLM 流式：下一个 chunk 边界停下，emit Cancelled(where=llm_stream)
              - 工具循环：当前工具跑完后停下（不打断已运行工具），emit Cancelled
                + RoundEnd(final=True)，返回已积累的部分答案
              - 进入新一轮 think 之前会 abort 整个循环
            没传则新建一个空 token——chat 内部自己用，不会被外部触发。

        中断后 chat() 仍正常返回（不抛 KeyboardInterrupt），让 REPL 平稳回到
        输入态。Cancelled 事件已通过 event_bus 通知前端"被中断了"。
        """
        token = cancel_token if cancel_token is not None else CancelToken()
        self.current_cancel_token = token
        # 让工具内部 get_current_cancel_token() 拿到这个 token；
        # ToolExecutor 的并发分支会 copy_context 给 worker 用同一份 ContextVar
        ctx_token = set_current_cancel_token(token)
        try:
            return self._chat_impl(
                user_query,
                token,
                attachments=attachments,
                persistent_user_text=persistent_user_text,
            )
        finally:
            reset_current_cancel_token(ctx_token)
            self.current_cancel_token = None

    async def chat_async(
        self,
        user_query: str,
        cancel_token: Optional[CancelToken] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        persistent_user_text: Optional[str] = None,
    ) -> str:
        """chat() 的 asyncio 包装。

        chat 内部走 OpenAI SDK 流式同步迭代器，不能原生 await，这里用
        asyncio.to_thread 把它丢到默认线程池。

        中断方式：
          - 推荐：直接调 cancel_token.cancel()。chat 在 worker 线程会按
            现有 token 检查路径在 chunk 边界 / 工具间停下，正常 return。
          - **不要**对返回的 task 调 task.cancel()——asyncio 只会让 await
            点抛 CancelledError，下面那个线程仍在跑（线程池不可中断）。
        """
        return await asyncio.to_thread(
            self.chat,
            user_query,
            cancel_token=cancel_token,
            attachments=attachments,
            persistent_user_text=persistent_user_text,
        )

    def _build_chat_messages(
        self,
        *,
        user_content: Any,
        system_instructions: str,
        memory_query: str = "",
    ) -> List[Dict[str, Any]]:
        """按当前 history/state 构造本轮初始 LLM messages。

        重构后流程(对齐 Claude Code):
        1. system_instructions 是 _build_system_instructions 返回的"运行时补充"段
           (Bash 权限/通讯平台、Skill 概览、运行时 UI 状态),作为 user-appended
           段加在新 system prompt 末尾。长期稳定的身份和行为规则已经集中在
           constant.system_prompt.ConstantSystemPrompt,并由 context static sections
           放在动态边界之前。
        2. get_system_prompt 异步组装完整 system prompt list[str](含 CLAUDE.md
           memory section、env_info、language 等)。
        3. build_system_prompt_blocks 切成带 cache scope 的 SystemPromptBlock。
        4. provider_adapter.emit_system 转成具体 API 字段(OpenAI 兼容是单 string)。
        5. 把本地 SessionState 文本作为单独的 user message 注入(取代旧的
           [State] packet 概念,语义更直白:"这些是上次会话的工作笔记")。
        6. 历史消息按 history_window 截尾,user_query 最后追加。
        """
        # 同步上下文里调 async,用 asyncio.run 起一个临时 loop。session 自身的
        # chat() 是 sync(由 cb_agents 的 OpenAI SDK 流式同步迭代器决定),所以
        # 每轮新建 event loop 是合理的;chat_async 走 to_thread 绕过 loop 冲突。
        # registry.list_tools() 直接返回 list[str](工具名),frozenset 一下用作
        # 缓存键的稳定输入。
        enabled_tools = frozenset(self.registry.list_tools())
        try:
            system_parts = asyncio.run(
                get_system_prompt(
                    enabled_tools=enabled_tools,
                    model=getattr(self.llm, "model", "") or "",
                    cwd=Path.cwd(),
                    memory_loader=self.memory_loader if self.ctx_enabled else None,
                    mcp_clients=self.mcp_clients,
                    skill_commands=self._collect_skill_commands(),
                    language=self.language,
                    memory_query=memory_query,
                )
            )
        except RuntimeError:
            # 已在 event loop 里(罕见,工具内调时):用 nest_asyncio 兼容
            loop = asyncio.new_event_loop()
            try:
                system_parts = loop.run_until_complete(
                    get_system_prompt(
                        enabled_tools=enabled_tools,
                        model=getattr(self.llm, "model", "") or "",
                        cwd=Path.cwd(),
                        memory_loader=self.memory_loader if self.ctx_enabled else None,
                        mcp_clients=self.mcp_clients,
                        skill_commands=self._collect_skill_commands(),
                        language=self.language,
                        memory_query=memory_query,
                    )
                )
            finally:
                loop.close()

        # 把项目自定义指令作为 user-appended 段追加到末尾
        if system_instructions and system_instructions.strip():
            system_parts.append(system_instructions.strip())

        # 切分 + 转 provider 格式
        model_id = getattr(self.llm, "model", "") or ""
        blocks = build_system_prompt_blocks(
            system_parts,
            use_global_cache_scope=should_use_global_cache_scope(model_id),
        )
        system_payload = self.provider_adapter.emit_system(blocks)

        # 组装最终 messages
        messages: List[Dict[str, Any]] = []
        if isinstance(system_payload, str):
            if system_payload.strip():
                messages.append({"role": "system", "content": system_payload})
        elif isinstance(system_payload, list):
            # Anthropic adapter 返回 list[dict];OpenAI 兼容路径走不到这里
            messages.append({"role": "system", "content": system_payload})

        # SessionState 作为独立 user message 注入(取代旧的 [State] packet)
        state_text = self._session_state_text()
        if state_text:
            messages.append({
                "role": "user",
                "content": (
                    "[本地工作态 / SessionState]\n"
                    "(以下是当前会话的滚动工作笔记,用于跨轮恢复;不是用户最新指令)\n\n"
                    + state_text
                ),
            })

        # 截尾历史
        # TODO：历史消息截尾逻辑需要优化，考虑是否需要根据安全窗口动态调整
        for m in self.history[-self.history_window:]:
            messages.append(m.to_dict())
        messages.append({"role": "user", "content": user_content})
        return messages

    def _collect_skill_commands(self) -> List[Any]:
        """从 SkillManager 收集 skill 命令,失败返回空列表。"""
        if self.skill_manager is None:
            return []
        try:
            list_fn = getattr(self.skill_manager, "list_commands", None)
            if callable(list_fn):
                return list(list_fn() or [])
        except Exception:
            logger.exception("skill_manager.list_commands 调用失败")
        return []

    def _chat_impl(
        self,
        user_query: str,
        token: CancelToken,
        attachments: Optional[List[Dict[str, Any]]] = None,
        persistent_user_text: Optional[str] = None,
    ) -> str:
        chat_started = time.perf_counter()
        # 后台任务完成通知 → 注入 user_query 前缀 + 发 BackgroundNotification 事件
        user_query = self._prepend_background_notifications(user_query)
        history_source_text = (
            str(persistent_user_text).strip()
            if persistent_user_text is not None
            else user_query
        ) # 从持久化用户文本或用户查询中获取历史文本
        multimodal_prompt = process_multimodal_prompt(
            text=user_query,
            attachments=attachments,
            model=getattr(self.llm, "model", None),
            history_text=history_source_text,
        ) # 处理多模态输入，生成请求内容和历史文本
        request_content = multimodal_prompt.request_content
        history_user_text = multimodal_prompt.history_text
        logger.info(
            "chat prepare: multimodal processed attachments=%s elapsed=%.2fs",
            len(multimodal_prompt.attachments),
            time.perf_counter() - chat_started,
        )
        if self.session_store is not None:
            try:
                # 通讯平台私聊会按“每条消息新建 AgentSession 对象”从磁盘恢复。
                # 因此收到用户消息后先写 pending 记录：如果进程在 LLM 返回前崩溃，
                # 下一次同一用户发消息时仍能从磁盘看到那条未完成输入。
                self.session_store.save_pending_user_message(history_user_text)
            except Exception:
                logger.exception("保存 pending 用户消息失败")

        stage_started = time.perf_counter() # 记录当前阶段开始时间
        system_instructions = self._build_system_instructions() # 构建运行时指令
        logger.info(
            "chat prepare: runtime instructions built chars=%s elapsed=%.2fs total=%.2fs",
            len(system_instructions or ""),
            time.perf_counter() - stage_started,
            time.perf_counter() - chat_started,
        )
        auto_compactions: List[Dict[str, Any]] = [] #收集自动压缩（auto compaction）事件
        messages = self._build_chat_messages(
            user_content=request_content,
            system_instructions=system_instructions,
            memory_query=history_user_text,
        )
        logger.info(
            "chat prepare: context messages built messages=%s elapsed=%.2fs total=%.2fs",
            len(messages),
            time.perf_counter() - stage_started,
            time.perf_counter() - chat_started,
        )

        stage_started = time.perf_counter()
        tools_schema = (
            self.registry.get_tools_description_openai_schema()
            if self.llm.is_Function_Calling
            else None
        )
        logger.info(
            "chat prepare: tools schema built tools=%s elapsed=%.2fs total=%.2fs",
            len(tools_schema or []),
            time.perf_counter() - stage_started,
            time.perf_counter() - chat_started,
        )
        logger.info(
            "chat start: query_chars=%s attachments=%s history=%s messages=%s tools=%s function_calling=%s",
            len(user_query),
            len(multimodal_prompt.attachments),
            len(self.history),
            len(messages),
            len(tools_schema or []),
            self.llm.is_Function_Calling,
        )
        logger.debug(
            "chat request estimate: tokens=%s context=%s",
            self._estimate_request_tokens(messages, tools_schema),
            self.context_window_usage(),
        )

        # 预检完整请求体，而不只看 state/history。原因是工具 schema、系统提示、
        # Skill 列表和当前用户输入也会占用真实模型窗口；如果这里达到或超过 80%
        # 安全窗口，就先 compact 当前跨轮 history/state，再重建 messages，让
        # 本轮第一次 think 就使用压缩后的上下文。
        preflight = self._maybe_auto_compact_preflight(
            user_query=user_query,
            system_instructions=system_instructions,
            messages=messages,
            tools_schema=tools_schema,
        )
        if preflight is not None:
            auto_compactions.append(preflight)
            logger.info("auto compact before first think: %s", preflight)
            messages = self._build_chat_messages(
                user_content=request_content,
                system_instructions=system_instructions,
                memory_query=history_user_text,
            )

        # 记录本轮初始消息（含 system/user/history）
        if self.message_logger is not None:
            try:
                self.message_logger.log(
                    sanitize_multimodal_payload(messages),
                    label=f"会话开始 | query=\"{history_user_text[:100]}\"",
                )
            except Exception:
                logger.exception("message_logger 写入失败")

        #工具调用次数，最终回答，工具轨迹，本轮压缩事件
        rounds_used, final_answer, trace_collector, loop_compactions = self._tool_loop(
            messages, tools_schema, token,
        )
        auto_compactions.extend(loop_compactions)

        # 跨轮历史仍然先保存用户输入和最终回答，保持原来的对话语义。
        # 下面的 work_record 是额外的普通 assistant 文本，不是 tool 消息。
        # 跨轮 history 只保存文本摘要，不保存本轮 request_content 里的 image_url/data URI。
        # 这保证 context_window_usage、/compact、session transcript 都不会被图片 base64 撑爆。
        self.history.append(Message.create_user_message(history_user_text))
        if final_answer:
            self.history.append(Message.create_assistant_message(final_answer))
        # trace_collector 来自本轮工具循环，里面只有被压缩过的工具事实。
        # 如果本轮没有工具调用，就不会生成【工作记录】，避免无意义地撑大 history。
        work_record = self._make_work_record(
            user_query=history_user_text,
            final_answer=final_answer,
            trace_collector=trace_collector,
        )
        if work_record is not None and work_record.text:
            self.history.append(make_work_record_message(work_record))
        self._auto_update_memory_and_knowledge(
            user_query=history_user_text,
            final_answer=final_answer,
            work_record_text=(work_record.text if work_record is not None else ""),
        )
        self._persist_turn(history_user_text, final_answer, work_record)

        # 本轮结束后再看一次跨轮 state/history。工具轨迹落盘和 state 合并可能让
        # 下一轮动态上下文达到或超过安全窗口；此时自动执行与 /compact 同语义的压缩，
        # transcript 仍保留审计，下一轮 prompt 则从 compact 快照继续。
        post_turn_compaction = self._maybe_auto_compact_history(
            reason="post_turn",
            round_idx=rounds_used,
        )
        if post_turn_compaction is not None:
            auto_compactions.append(post_turn_compaction)
            logger.info("auto compact after turn: %s", post_turn_compaction)

        # Done 事件：让前端知道整轮结束
        elapsed = time.perf_counter() - chat_started
        logger.info(
            "chat done: rounds=%s cancelled=%s answer_chars=%s auto_compactions=%s elapsed=%.2fs",
            rounds_used,
            token.is_cancelled(),
            len(final_answer or ""),
            len(auto_compactions),
            elapsed,
        )
        self.event_bus.emit(Done(
            final_answer=final_answer,
            rounds_used=rounds_used,
            cancelled=token.is_cancelled(),
            context_window=self.context_window_usage(),
            auto_compact={
                "compacted": bool(auto_compactions),
                "events": auto_compactions,
            } if auto_compactions else None,
        ))
        return final_answer

    def clear_history(self) -> None:
        # /clear 的新语义是"彻底清理当前会话":内存 history 和项目级
        # .cbagent/sessions active session 都删掉。下一轮继续聊天时 store 会
        # 自动创建新 session,不会把旧上下文再恢复回来。
        # 同时清空 SystemPromptSectionCache 与 MemoryLoader memoize,让下一轮
        # 重读 CLAUDE.md / 重算 env_info(用户编辑了记忆文件后能立刻生效)。
        self.history.clear()
        if self.session_store is not None:
            try:
                self.session_store.clear_active_session()
            except Exception:
                logger.exception("清理本地会话失败")
        clear_system_prompt_sections()
        if self.memory_loader is not None:
            try:
                self.memory_loader.reset_cache(reason="clear_history")
            except Exception:
                logger.exception("MemoryLoader 缓存清理失败")

    def _model_max_tokens(self) -> int:
        """返回当前模型声明的完整上下文窗口。

        这里不再读 ContextBuilder.config.max_tokens 作为主来源，因为用户已经把
        各模型真实窗口统一维护在 ``constant/llm/constant_llm.py``。ContextBuilder
        只是“如何组织上下文”的组件，模型窗口是 LLM 配置的一部分，两者拆开后，
        切换模型时状态栏和自动 compact 不会被旧的 8000 默认值误导。
        """
        return ConstantLLM.model_max_tokens(getattr(self.llm, "model", None))

    def _context_budget_tokens(self) -> int:
        """返回 agent 实际可使用的上下文窗口，默认是模型窗口的 80%。

        这个值同时服务三个地方：
        - TUI 底部 Context 指标的分母；
        - 自动 compact 的触发阈值；
        - 估算当前工具循环 messages 是否需要压缩。

        保留 20% 不使用，是为了给模型输出、provider 额外包装和 token 估算误差
        留缓冲，避免显示“刚好没满”但真实 API 请求已经超窗。
        """
        return ConstantLLM.context_window_tokens(getattr(self.llm, "model", None))

    def _dynamic_context_text(self) -> str:
        """渲染会被跨轮注入的动态上下文文本，用于估算和自动 compact。

        它只统计 state/history，不统计固定 system prompt、工具 schema 和当前用户
        输入。因此这个结果适合回答“这段会话记忆本身占了多少窗口”；完整请求是否
        超阈值则由 _estimate_request_tokens() 另外计算。
        """
        parts: List[str] = []
        state_text = self._session_state_text()
        if state_text:
            parts.append("[State]\n" + state_text)

        history_lines = [
            _context_message_line(message)
            for message in self.history[-self.history_window:]
            if _message_content_to_text(message.content)
        ]
        if history_lines:
            parts.append("[Context]\n" + "\n".join(history_lines))
        return "\n\n".join(parts)

    def context_window_usage(self) -> Dict[str, Any]:
        """估算当前会话动态上下文占用，用于 TUI 的 Context 指标。

        这个指标回答的是“当前 active 会话已有多少内容会继续挤占后续上下文窗口”，
        不是 OpenAI usage 里的“已经消耗了多少 token”。因此它只统计动态部分：

        - 本地滚动 state，也就是已读/已改文件、最近命令、compact 摘要等；
        - 当前恢复进内存的 history，包括普通 user/assistant、工作记录和 compact 记录。

        固定系统提示、工具 schema、Skill 列表以及下一条尚未提交的用户输入不计入。
        这样空会话会接近 0%，切换会话和 /compact 后的变化也更直观。分母使用
        ``constant_llm.py`` 中当前模型 max_tokens 的 80%，与自动 compact 阈值一致。
        """
        model_max_tokens = self._model_max_tokens()
        max_tokens = self._context_budget_tokens()
        text = self._dynamic_context_text()
        used_tokens = count_tokens(text) if text else 0
        percent = min(100.0, (used_tokens / max_tokens) * 100.0)
        return {
            "used_tokens": used_tokens,
            "max_tokens": max_tokens,
            "remaining_tokens": max(0, max_tokens - used_tokens),
            "percent": round(percent, 1),
            "source": "estimate",
            "scope": "state+history",
            "model_max_tokens": model_max_tokens,
            "threshold_ratio": ConstantLLM.CONTEXT_USAGE_RATIO,
        }

    def _estimate_request_tokens(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]] = None,
    ) -> int:
        """粗略估算一次 LLM 请求的 token 数。

        自动 compact 不能只看 history，因为真实请求还包含 system prompt、当前
        user query、工具 schema，以及本轮 tool loop 累积的 assistant/tool 消息。
        这里把 messages/tools_schema 序列化为 JSON 后统一计数，结果不是 provider
        的精确 token accounting，但足以做“是否接近窗口”的保守触发判断。
        """
        payload = {
            # 当前轮请求可能包含 image_url data URI。token 估算和自动 compact 只需要
            # 知道“这里有图片”，不应该把 base64 当成长期上下文文本来计数。
            "messages": sanitize_multimodal_payload(messages),
            "tools": tools_schema or [],
        }
        try:
            text = json.dumps(payload, ensure_ascii=False, default=str)
        except Exception:
            text = str(payload)
        return count_tokens(text)

    def _auto_compact_history(
        self,
        *,
        reason: str,
        round_idx: int = 0,
        force: bool = False,
        request_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """执行一次自动跨轮 compact，并返回审计用轻量事件。

        与手动 /compact 一样，它只重写 ``self.history`` 和当前 session 的
        compact.json/state.json；不会删除 transcript，也不会向 history 里写 role=tool。
        ``force=True`` 用于 preflight：即使 state/history 自身没有达到 80% 窗口，
        只要完整请求体超阈值，也尝试压缩可压缩的跨轮历史。
        """
        before_usage = self.context_window_usage()
        budget = int(before_usage["max_tokens"])
        if not force and int(before_usage["used_tokens"]) < budget:
            return None
        before_messages = len(self.history)
        state_text = self._session_state_text()
        if before_messages == 0 and not state_text:
            return None

        try:
            payload = self.compact_context()
        except Exception:
            logger.exception("自动上下文 compact 失败")
            return None
        if payload.get("no_op"):
            return None

        after_usage = payload.get("context_window") or self.context_window_usage()
        return {
            "reason": reason,
            "round_idx": round_idx,
            "before_messages": before_messages,
            "after_messages": int(payload.get("after_messages") or len(self.history)),
            "before_tokens": int(before_usage.get("used_tokens") or 0),
            "after_tokens": int(after_usage.get("used_tokens") or 0),
            "budget_tokens": budget,
            "request_tokens": request_tokens,
            "persisted": bool(payload.get("persisted")),
        }

    def _maybe_auto_compact_history(
        self,
        *,
        reason: str,
        round_idx: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """当跨轮 state/history 达到或超过 80% 安全窗口时自动 compact。"""
        return self._auto_compact_history(reason=reason, round_idx=round_idx, force=False)

    def _maybe_auto_compact_preflight(
        self,
        *,
        user_query: str,
        system_instructions: str,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        """本轮第一次 think 前，按完整请求体判断是否先压缩跨轮历史。

        如果请求体已经达到或超过模型窗口 80%，即便动态 history 单独看还没达到，也说明
        system prompt/工具 schema/当前输入叠加后空间紧张。此时优先压缩 history，
        然后由 _chat_impl 重建 messages，让本轮第一次请求就变小。
        """
        del user_query, system_instructions  # 仅用于调用点自解释，估算直接读 messages。
        request_tokens = self._estimate_request_tokens(messages, tools_schema)
        budget = self._context_budget_tokens()
        if request_tokens < budget:
            return None
        return self._auto_compact_history(
            reason="preflight",
            round_idx=0,
            force=True,
            request_tokens=request_tokens,
        )

    def _tool_result_message_summary(
        self,
        *,
        name: str,
        trace_line: str,
    ) -> str:
        """生成替换当前轮 tool message content 的安全摘要。

        这一步只发生在当前工具循环请求体达到或超过 80% 窗口时。它不会影响
        TraceCollector 里的跨轮事实，也不会写完整文件正文/stdout 到磁盘；只是把
        下一次 ``llm.think(messages=...)`` 里的 tool content 从“完整输出”换成
        “已压缩摘要”，以保住 tool_call_id 配对同时释放上下文。
        """
        summary = (
            "【自动工具结果压缩】当前工具循环已接近上下文窗口，"
            f"工具 {name} 的完整输出已替换为摘要：{trace_line}"
        )
        return _clip_compact_text(summary, self.AUTO_TOOL_MESSAGE_LIMIT)

    def _maybe_compress_tool_loop_messages(
        self,
        *,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
        tool_message_summaries: Dict[int, str],
        compressed_indices: set[int],
        round_idx: int,
    ) -> Optional[Dict[str, Any]]:
        """当前轮 messages 达到或超过 80% 窗口时，压缩已完成的 tool result。

        这里故意不删除 assistant/tool 消息，也不把 tool role 改成 assistant。
        OpenAI tool calling 协议要求 assistant.tool_calls 后必须有匹配的 tool
        message；直接删改 role 会导致下一次请求 400。替换 content 则能同时满足
        协议和上下文压缩需求。
        """
        budget = self._context_budget_tokens()
        before_tokens = self._estimate_request_tokens(messages, tools_schema)
        if before_tokens < budget:
            return None

        compressed_count = 0
        for idx, summary in tool_message_summaries.items():
            if idx in compressed_indices:
                continue
            if idx < 0 or idx >= len(messages):
                continue
            if messages[idx].get("role") != "tool":
                continue
            messages[idx]["content"] = summary
            compressed_indices.add(idx)
            compressed_count += 1

        # 当前轮压缩 tool content 只影响正在跑的局部 messages；为了让下一轮也轻，
        # 同时尝试 compact 跨轮 history/state。它不会改变当前 messages，但会更新
        # Done 之后 TUI 看到的 context_window 和本地 compact 快照。
        history_event = self._auto_compact_history(
            reason="tool_loop_history",
            round_idx=round_idx,
            force=True,
            request_tokens=before_tokens,
        )
        after_tokens = self._estimate_request_tokens(messages, tools_schema)
        if compressed_count == 0 and history_event is None:
            return None
        return {
            "reason": "tool_loop",
            "round_idx": round_idx,
            "before_tokens": before_tokens,
            "after_tokens": after_tokens,
            "budget_tokens": budget,
            "compressed_tool_messages": compressed_count,
            "history_compaction": history_event,
        }

    # ---------- 工具循环 ----------

    def _tool_loop(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
        token: CancelToken,
    ) -> tuple[int, str, TraceCollector, List[Dict[str, Any]]]:
        """工具调用主循环。返回 (rounds_used, final_answer, trace_collector, auto_compactions)。

        每轮：
        1. 检查 token：进新一轮前已被 cancel → 立刻收尾
        2. emit RoundStart
        3. llm.think(event_bus=self.event_bus, cancel_event=token.event)
        4. 若有 tool_calls：assistant 回灌 → executor.execute → tool 回灌
           → emit RoundEnd(has_tool_calls=True)。期间 token 被 set 后，
           ToolExecutor 在工具间会跳过未跑的并 emit Cancelled
        5. 若没 tool_calls：emit RoundEnd(final=True)，返回 answer
        6. 超过 MAX_TOOL_ROUNDS 仍未收敛 → emit Error 并兜底
        """
        partial_answer = ""  # 中断时已经流式打了一部分答案，要回传给前端
        # trace_collector 与 messages 并行存在：
        # - messages：完整协议上下文，包含完整 tool result，用于本轮继续 think；
        # - trace_collector：跨轮压缩轨迹，只记录 path/cwd/exit_code/短摘要等。
        # 这保证了本轮推理能力不受截断影响，同时下一轮不会背上大段工具输出。
        trace_collector = TraceCollector()
        # 当前轮 messages 的自动压缩事件会在 chat 结束时放进 Done.auto_compact，
        # 方便 TUI 或日志侧知道“这轮为了保护上下文窗口做过压缩”。
        loop_compactions: List[Dict[str, Any]] = []  # 当前轮自动压缩事件
        # tool message content 只有在当前请求体达到或超过 80% 安全窗口时才会被替换。
        # key 是 messages 里的下标，value 是预先从 TraceEntry 生成的安全摘要。
        tool_message_summaries: Dict[int, str] = {}  
        compressed_tool_message_indices: set[int] = set()  # 已压缩的 tool message 下标，用于去重
        for round_idx in range(1, self.MAX_TOOL_ROUNDS + 1):
            # 进入新一轮前先看 token
            if token.is_cancelled():
                self.event_bus.emit(Cancelled(
                    where="session_loop", round_idx=round_idx,
                ))
                self.event_bus.emit(RoundEnd(
                    round_idx=max(round_idx - 1, 1),
                    has_tool_calls=False, final=True,
                ))
                return (
                    round_idx - 1 if round_idx > 1 else 1,
                    partial_answer,
                    trace_collector,
                    loop_compactions,
                )

            self.event_bus.emit(RoundStart(
                round_idx=round_idx,
                max_rounds=self.MAX_TOOL_ROUNDS,
            ))
            logger.info(
                "round start: round=%s messages=%s request_tokens_est=%s",
                round_idx,
                len(messages),
                self._estimate_request_tokens(messages, tools_schema),
            )
            if self.messages_snapshot_hook is not None:
                try:
                    # CLI 的 /msg dump 是落到终端/日志的调试视图，只展示脱敏副本。
                    self.messages_snapshot_hook(sanitize_multimodal_payload(messages), round_idx)
                except Exception:
                    logger.exception("messages_snapshot_hook 抛异常，已吞")

            if self.message_logger is not None:
                try:
                    self.message_logger.log(
                        sanitize_multimodal_payload(messages),
                        label=f"第 {round_idx} 轮 think 前",
                    )
                except Exception:
                    logger.exception("message_logger 写入失败")

            result = self.llm.think(
                messages,
                tools=tools_schema,
                event_bus=self.event_bus,
                cancel_event=token.event,
                round_idx=round_idx,
            )

            # 不支持 FC 的模型返回 [text, None]
            if isinstance(result, list):
                final = result[0] or ""
                logger.info("round final without tools: round=%s answer_chars=%s", round_idx, len(final))
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, final, trace_collector, loop_compactions

            if not isinstance(result, dict):
                #TODO：错误信息不明确
                logger.error("LLM returned unexpected result: round=%s type=%s", round_idx, type(result).__name__)
                self.event_bus.emit(Error(
                    where="llm",
                    message=f"模型返回非预期结构: {type(result).__name__}",
                    round_idx=round_idx,
                ))
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, "", trace_collector, loop_compactions

            answer = result.get("answer", "") or ""
            tool_calls = result.get("tool_calls") or []
            reasoning = result.get("reasoning_content")
            # 流式中途被 cancel：cb_agents 已 emit Cancelled，answer 是已收的部分
            if answer:
                partial_answer = answer

            # 流式过程中被 cancel → 不再发起新一轮工具调用，直接收尾
            if token.is_cancelled():
                logger.info("round cancelled after llm stream: round=%s answer_chars=%s", round_idx, len(answer))
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, answer, trace_collector, loop_compactions

            if not tool_calls:
                logger.info("round final: round=%s answer_chars=%s", round_idx, len(answer))
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, answer, trace_collector, loop_compactions

            # assistant 的 tool_calls 消息回灌
            assistant_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": answer or None,
                "tool_calls": tool_calls,
            }
            if reasoning:
                # thinking 模式要求 reasoning_content 回传，否则下一轮 400
                assistant_msg["reasoning_content"] = reasoning
            messages.append(assistant_msg)
            tool_names = [
                call.get("function", {}).get("name", "?")
                for call in tool_calls
            ]
            logger.info(
                "round planned tool calls: round=%s count=%s tools=%s answer_chars=%s reasoning_chars=%s",
                round_idx,
                len(tool_calls),
                tool_names,
                len(answer),
                len(reasoning or ""),
            )

            # 调度执行（事件由 ToolExecutor 自己 emit ToolStart/ToolComplete）
            # token 透传给 executor：串行/并发模式下都在工具间做 cancel 检查
            results = self.executor.execute(
                tool_calls, round_idx=round_idx, cancel_token=token,
            )
            for call, exec_result in zip(tool_calls, results):
                # 完整工具结果仍按 OpenAI tool calling 协议回灌给本轮 messages。
                # 这一点不能改，否则多轮工具调用时模型看不到真实工具输出。
                tool_message_idx = len(messages)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id", ""),
                    "name": exec_result.name,
                    "content": (
                        exec_result.result
                        if isinstance(exec_result.result, str)
                        else str(exec_result.result)
                    ),
                })
                # 另一路只记录压缩摘要，供本轮结束后生成【工作记录】。
                # 这里不把 trace 写入 messages，避免下一轮出现伪造的 tool 消息。
                trace_entry = trace_collector.add_tool_result(
                    call=call,
                    name=exec_result.name,
                    result=exec_result.result,
                    is_error=exec_result.is_error,
                    round_idx=round_idx,
                )
                tool_message_summaries[tool_message_idx] = self._tool_result_message_summary(
                    name=exec_result.name,
                    trace_line=trace_entry.to_line(),
                )

            compact_event = self._maybe_compress_tool_loop_messages(
                messages=messages,
                tools_schema=tools_schema,
                tool_message_summaries=tool_message_summaries,
                compressed_indices=compressed_tool_message_indices,
                round_idx=round_idx,
            )
            if compact_event is not None:
                loop_compactions.append(compact_event)
                logger.info("tool loop compacted messages: %s", compact_event)

            self.event_bus.emit(RoundEnd(
                round_idx=round_idx, has_tool_calls=True, final=False,
            ))
            logger.info("round end with tools: round=%s tool_results=%s", round_idx, len(results))

        # 超出最大轮数
        self.event_bus.emit(Error(
            where="session",
            message=f"工具调用超过 {self.MAX_TOOL_ROUNDS} 轮，强制终止",
            round_idx=self.MAX_TOOL_ROUNDS,
        ))
        return (
            self.MAX_TOOL_ROUNDS,
            "（工具调用次数过多，已终止本轮）",
            trace_collector,
            loop_compactions,
        )

    # ---------- 辅助 ----------

    def _session_state_text(self) -> str:
        """读取当前本地会话 state 的可注入文本。

        这个方法和 _build_state_packet() 的读取逻辑保持一致，但返回纯字符串，
        供 /compact 摘要器使用。它吞掉 store 读取异常，是因为 compact 属于管理
        命令：state 读失败时仍可压缩内存 history，不能让一次磁盘异常阻断用户。
        """
        if self.session_store is None:
            return ""
        try:
            return self.session_store.state_text() or ""
        except Exception:
            logger.exception("本地会话状态读取失败")
            return ""

    def _latest_plain_turn_messages(self) -> List[Message]:
        """取最近一轮普通 user/assistant 对话。

        history 里现在可能混有三类 assistant：
        - 给用户看的最终回答；
        - `【工作记录】`，kind=work_record；
        - `【上下文压缩】`，kind=compact_record。

        /compact 后只保留真正的最近一轮用户/助手对话，工作记录和 compact 锚点
        已经被折进新的摘要里，继续保留它们会浪费上下文窗口。
        """
        # TODO: 改为按 token 预算保留多轮对话，而非固定只保留最近 1 轮。
        #   方案：从最近往前按完整轮次填充，直到 context_window * RETAIN_RATIO(~0.18) 用完。
        #   以轮次为单位不切断，第一轮无条件保留，只保留 user/assistant 纯对话。
        retained: List[Message] = []
        last_plain_role = ""
        for message in reversed(self.history):
            if _message_kind(message):
                continue
            role = _message_role_name(message)
            if role not in {"user", "assistant"}:
                continue
            if not retained:
                retained.append(message)
                last_plain_role = role
                if role == "user":
                    break
                continue
            if last_plain_role == "assistant" and role == "user":
                retained.append(message)
                break
        return list(reversed(retained))

    def _history_text_for_compact(
        self,
        messages: Optional[Sequence[Message]] = None,
    ) -> str:
        """把当前内存 history 渲染成 compact summarizer 的输入文本。

        这里读取的是普通跨轮 history，而不是本轮 tool loop 的 messages，因此不会
        出现 role=tool 的完整工具输出。每条消息仍然做短截断，最后整体再截断，
        防止用户/助手长文回答让静默 summarizer 的输入过大。
        """
        lines: List[str] = []
        source = self.history if messages is None else messages
        for message in source:
            role = _message_role_name(message)
            kind = _message_kind(message)
            content = _clip_compact_text(_message_content_to_text(message.content), 500)
            if not content:
                continue
            label = role if not kind else f"{role}/{kind}"
            lines.append(f"{label}: {content}")
        return _clip_compact_text("\n".join(lines), 12000)

    def _state_snapshot_for_compact(self, state_text: str) -> str:
        """把 state.json 中的关键结构化状态渲染给 compact summarizer。

        LocalSessionStore.state_text() 已经覆盖 rolling_summary、文件、命令和待办；
        这里再补 active_task/decisions 等字段，让 LLM 和规则兜底都能看到计划要求
        的“当前任务、关键结论、待办/阻塞”。
        """
        if self.session_store is None:
            return state_text
        state = self.session_store.state if isinstance(self.session_store.state, dict) else {}
        parts: List[str] = []
        active_task = _clip_compact_text(state.get("active_task"), 240)
        if active_task:
            parts.append("当前任务：" + active_task)
        if state_text:
            parts.append("滚动状态：\n" + _clip_compact_text(state_text, 4000))
        decisions = state.get("decisions") if isinstance(state.get("decisions"), list) else []
        if decisions:
            parts.append("关键结论：" + "；".join(_clip_compact_text(x, 120) for x in decisions[-8:]))
        pending = state.get("pending") if isinstance(state.get("pending"), list) else []
        if pending:
            parts.append("待办/阻塞：" + "；".join(_clip_compact_text(x, 120) for x in pending[-8:]))
        return _clip_compact_text("\n".join(parts), 6000)

    def _make_compact_summary(
        self,
        *,
        messages: Optional[Sequence[Message]] = None,
        state_text: str,
        focus: Optional[str] = None,
    ) -> str:
        """生成 /compact 摘要文本。

        优先走 OpenAI-compatible client 的非流式静默调用；它不会经过 llm.think，
        因此不会向 EventBus 发 TextDelta/ReasoningDelta，也不会在 TUI 中显示成
        一条助手回答。任何失败都会回退到规则摘要，保证 /compact 是可靠的管理
        操作，而不是依赖网络/模型可用性的脆弱路径。
        """
        fallback = self._rule_compact_summary(
            messages=messages,
            state_text=state_text,
            focus=focus,
        )
        client = getattr(self.llm, "client", None)
        model = getattr(self.llm, "model", None)
        if client is None or not model:
            return fallback
        focus_text = f"\n摘要关注主题：{focus}" if focus else ""

        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0,
                stream=False,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是 cb-agent 的上下文压缩器。请把当前会话历史和工作状态压缩成"
                            "一条中文摘要，保留后续继续任务必须知道的信息：当前任务、用户偏好、"
                            "关键结论、已读文件、已改文件、最近命令、待办/阻塞。不要编造。"
                            "输出不超过1200字，并以【上下文压缩】开头。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "[当前会话历史]\n"
                            f"{self._history_text_for_compact(messages)}\n\n"
                            "[本地滚动状态]\n"
                            f"{self._state_snapshot_for_compact(state_text)}"
                            f"{focus_text}"
                        ),
                    },
                ],
            )
            content = response.choices[0].message.content or ""
        except Exception:
            logger.exception("silent context compaction failed")
            return fallback

        content = _clip_compact_text(content, COMPACT_RECORD_LIMIT)
        if not content:
            return fallback
        if not content.startswith("【上下文压缩】"):
            content = "【上下文压缩】" + content
        return _clip_compact_text(content, COMPACT_RECORD_LIMIT)

    def _rule_compact_summary(
        self,
        *,
        messages: Optional[Sequence[Message]] = None,
        state_text: str,
        focus: Optional[str] = None,
    ) -> str:
        """无 LLM 或 LLM 失败时的规则 compact 摘要。

        规则摘要不尝试推断新事实，只把已经存在于 history/state 里的内容重新组织
        成短文本。这样可以最大限度降低“压缩时编造”的风险，同时仍然释放大部分
        近轮对话窗口。
        """
        state_snapshot = self._state_snapshot_for_compact(state_text)
        history_text = self._history_text_for_compact(messages)
        parts = ["【上下文压缩】"]
        if focus:
            parts.append("关注主题：" + _clip_compact_text(focus, 120))
        if state_snapshot:
            parts.append("状态摘要：" + _clip_compact_text(state_snapshot, 700))
        if history_text:
            parts.append("最近对话：" + _clip_compact_text(history_text, 500))
        if len(parts) == 1:
            parts.append("当前没有可压缩的有效上下文。")
        return _clip_compact_text("\n".join(parts), COMPACT_RECORD_LIMIT)

    def _make_work_record(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_collector: TraceCollector,
    ):
        """把本轮压缩工具轨迹转换成一条 WorkRecord。

        小 trace 直接用规则总结；大 trace 优先走静默 LLM 总结。无论哪条路径
        失败，都回退到规则总结，并且不影响本轮最终回答和 Done 事件。
        """
        if not trace_collector.entries:
            return None
        try:
            if trace_collector.needs_summary() and self.trace_summarizer is not None:
                return self.trace_summarizer.summarize(
                    user_query=user_query,
                    final_answer=final_answer,
                    trace_entries=trace_collector.entries,
                )
            return self.rule_trace_summarizer.summarize(
                user_query=user_query,
                final_answer=final_answer,
                trace_entries=trace_collector.entries,
            )
        except Exception:
            logger.exception("工具轨迹总结失败，使用规则压缩兜底")
            return self.rule_trace_summarizer.summarize(
                user_query=user_query,
                final_answer=final_answer,
                trace_entries=trace_collector.entries,
            )

    def _persist_turn(self, user_query: str, final_answer: str, work_record) -> None:
        """把本轮对话和工作记录写入项目级 session store。"""
        if self.session_store is None:
            return
        try:
            self.session_store.append_turn(
                user_query=user_query,
                final_answer=final_answer,
                work_record=work_record,
            )
        except Exception:
            logger.exception("本地会话落盘失败")

    def _auto_update_memory_and_knowledge(
        self,
        *,
        user_query: str,
        final_answer: str,
        work_record_text: str = "",
    ) -> None:
        """Best-effort long-term memory and structured knowledge update."""
        if self.memory_loader is None or not self.ctx_enabled:
            return
        record_turn = getattr(self.memory_loader, "record_turn", None)
        if not callable(record_turn):
            return
        try:
            result = record_turn(
                user_text=user_query,
                assistant_text=final_answer or "",
                work_record_text=work_record_text or "",
            )
            if result is not None:
                logger.debug(
                    "memory/knowledge auto-update: memory=%s pages=%s errors=%s",
                    getattr(result, "memory_updated", False),
                    len(getattr(result, "pages", []) or []),
                    getattr(result, "errors", []) or [],
                )
        except Exception:
            logger.exception("memory/knowledge auto-update failed")

    def _prepend_background_notifications(self, user_query: str) -> str:
        """每轮 chat 前 drain 后台任务通知，挂到 user_query 前作为 system reminder。

        同时为每条通知 emit 一个 BackgroundNotification 事件，前端可独立渲染。
        """
        try:
            from tools.tools.bash_background import get_background_registry
            done = get_background_registry().drain_notifications()
        except Exception:
            return user_query
        if not done:
            return user_query

        for t in done:
            self.event_bus.emit(BackgroundNotification(
                task_id=str(t.id),
                status=t.status,
                exit_code=t.exit_code,
                output_path=t.output_path,
            ))

        lines = ["<system-reminder>", "[后台任务完成通知]"]
        for t in done:
            lines.append(
                f"- task_id={t.id} status={t.status} exit={t.exit_code} "
                f"cmd={t.command!r} output={t.output_path}"
            )
        lines.append(
            "请在回答用户前主动用 bash_task(action=output, task_id=...) "
            "拉一下完成任务的结果，告知用户。"
        )
        lines.append("</system-reminder>")
        return "\n".join(lines) + "\n\n" + user_query

    def _build_system_instructions(self) -> str:
        """组装运行时 system prompt 补充段：Bash / Skill。

        固定身份、行为规则和用户 cosplay 风格已经放在
        ``constant.system_prompt.ConstantSystemPrompt``。这里刻意只拼运行态内容，
        这样未来启用 provider prompt cache 时，稳定前缀不会被 Bash 权限模式、
        Skill 列表或运行时 UI 状态这些易变信息破坏命中率。
        """
        parts: list[str] = []

        if self.bash_prompt_provider is not None:
            try:
                bash_prompt = self.bash_prompt_provider()
                if bash_prompt:
                    parts.append("")
                    parts.append(bash_prompt)
            except Exception:
                logger.exception("bash_prompt_provider 调用失败")

        if self.skill_manager is not None:
            try:
                overview = self.skill_manager.build_skills_overview(max_chars=1500)
                if overview:
                    parts.append("")
                    parts.append(overview)
            except Exception:
                logger.exception("skill overview 构建失败")

        return "\n".join(parts)


__all__ = ["AgentSession"]
