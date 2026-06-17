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
from agent.compact_boundary import (
    COMPACT_BOUNDARY_KIND,
    get_messages_after_compact_boundary,
    make_compact_boundary_message,
)
from agent.microcompact import apply_microcompact
from agent.message_protocol import drop_orphan_tool_messages
from agent.work_context import (
    COMPACT_RECORD_LIMIT,
    LocalSessionStore,
    RuleTraceSummarizer,
    TraceCollector,
    TraceSummarizer,
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


def _llm_result_to_assistant_payload(result: Any) -> Optional[Dict[str, Any]]:
    """Convert an LLM result into an assistant-role log payload."""
    if isinstance(result, list):
        return {"role": "assistant", "content": result[0] if result else ""}
    if not isinstance(result, dict):
        return None
    answer = result.get("answer") or ""
    tool_calls = result.get("tool_calls") or []
    reasoning = result.get("reasoning_content")
    payload: Dict[str, Any] = {
        "role": "assistant",
        "content": answer or None,
    }
    if tool_calls:
        payload["tool_calls"] = tool_calls
    if reasoning:
        payload["reasoning_content"] = reasoning
    return payload


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


class AgentSession:
    """单个 agent 会话。一个进程里通常只有一个，但多会话场景也支持。

    构造时把所有依赖注入进来；运行时只暴露 chat() 一个入口。
    """

    # 工具调用循环最大轮数，防死循环
    MAX_TOOL_ROUNDS = 200

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
        hook_manager: Optional[Any] = None,
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
        # 可选 HookManager：在用户提交、会话开始、上下文压缩、收尾等生命周期点
        # 触发用户可配置的 hook。None 表示不启用 hooks（零回归）。
        self.hook_manager = hook_manager
        # SessionStart 只在「本会话首个 Prompt」触发一次，这个标志做去重。
        self._session_start_fired = False
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
        # 切换会话与 /clear 一样要清掉 system prompt section 缓存和 MemoryLoader
        # memoize：env_info 的缓存键含 cwd，CLAUDE.md memory 段也按上一会话状态
        # 缓存过。换会话(尤其换项目目录)后若不清，下一轮可能注入上一会话的
        # 环境快照或记忆。clear_history 已经这样做，这里保持一致。
        clear_system_prompt_sections()
        if self.memory_loader is not None:
            try:
                self.memory_loader.reset_cache(reason="switch_session")
            except Exception:
                logger.exception("MemoryLoader 缓存清理失败")
        return {
            "session": summary,
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
        }

    def compact_context(self) -> Dict[str, Any]:
        """压缩当前会话上下文，释放下一轮 prompt 的近轮历史占用。

        /compact 和 /clear 的语义不同：
        - /clear 是彻底删除当前 session 文件并清空内存；
        - /compact 保留 transcript 审计，在 history 末尾追加一条 compact_boundary
          消息(system 角色,kind=compact_boundary)。下一轮 _build_chat_messages
          会用 get_messages_after_compact_boundary 切片,只把 boundary 之后
          (含 boundary)的消息发给 LLM,boundary 之前的原始消息留在 history 用于
          审计/恢复但不再注入 prompt。

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
        boundary = make_compact_boundary_message(summary)
        # 在 history 末尾追加 boundary。注意:不删除 boundary 之前的消息——
        # 它们仍保留用于审计、恢复和未来可能的二次 compact;只是下一轮发给
        # LLM 时通过切片忽略掉。
        # 持久化失败时回滚 boundary,保证内存 history 与磁盘一致。
        self.history.append(boundary)
        after_messages = len(self.history)

        # 持久化:save_compaction 的 history_payload 保存"recovery 用的最近消息",
        # CC 模式下这是 boundary(含)之后的所有消息。下次启动时 load_latest_history
        # 优先读这个快照,确保跨进程也能从 compact 之后继续。
        persisted = False
        if self.session_store is not None:
            try:
                from agent.work_context import _message_to_persist_payload
                self.session_store.save_compaction(
                    summary=str(boundary.content or ""),
                    history_payload=[
                        _message_to_persist_payload(boundary),
                    ],
                    before_messages=before_messages,
                    after_messages=after_messages,
                )
                persisted = True
            except Exception:
                # 落盘失败:回滚 boundary,内存 history 与磁盘保持一致。
                self.history.pop()
                logger.exception("本地会话 compact 快照落盘失败")
                raise

        return {
            "session": self.current_session_payload().get("session"),
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
            "summary": str(boundary.content or ""),
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

        # 截尾历史 —— CC 模式：切片 + window + 孤儿清理统一收敛到
        # _sliced_history_dicts()，确保"发给 LLM 的请求"与"展示给用户的
        # Context%"(context_window_usage)算的是同一批消息。
        messages.extend(self._sliced_history_dicts())
        messages.append({"role": "user", "content": user_content})
        # microcompact 必须在 user_query 追加之后调用,因为它根据 messages 中
        # role=tool 的总数判定阈值,统计窗口要包含本轮发给 LLM 的全部消息。
        apply_microcompact(messages)
        return messages

    def _sliced_history_dicts(self) -> List[Dict[str, Any]]:
        """按 CC 模式构造跨轮 history 的 OpenAI dict 列表。

        三步,与真正发给 LLM 的口径完全一致:
        1. boundary 切片:取最后一个 compact_boundary 之后(含)的消息;无
           boundary 时返回全部 history。boundary 之前的原始消息留在 history
           用于审计/恢复,但不再进入下一轮 prompt。
        2. history_window 限位:再取尾部 N 条,防止极长会话整段灌入。
        3. 孤儿清理:window 截断这一刀可能正好落在 assistant(tool_calls) 和它
           的 tool 响应之间,导致切片开头出现"无父" tool 消息。OpenAI 兼容
           协议会因此报 400,这里把这类孤儿丢弃,保证请求体合法。

        microcompact 不在这里做:它依赖"本轮全部消息(含当前 user_query)"的
        tool_result 计数,只能在 _build_chat_messages 拼完后调用。
        """
        sliced = get_messages_after_compact_boundary(self.history)
        dicts = [m.to_dict() for m in sliced[-self.history_window:]]
        drop_orphan_tool_messages(dicts)
        return dicts

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

        # 生命周期 hook：SessionStart（本会话首个 Prompt，仅一次）+ UserPromptSubmit。
        # 两者的 additional_context 收集起来，稍后追加进 system_instructions 注入模型；
        # UserPromptSubmit blocked 则直接返回拒绝原因，不进 LLM。
        hook_extra_context = ""
        if self.hook_manager is not None:
            if not self._session_start_fired:
                self._session_start_fired = True
                if self.hook_manager.has_event("SessionStart"):
                    ss = self.hook_manager.fire(
                        "SessionStart",
                        {"source": "startup"},
                        matcher_value="startup",
                    )
                    if ss.additional_context:
                        hook_extra_context += ss.additional_context
            if self.hook_manager.has_event("UserPromptSubmit"):
                ups = self.hook_manager.fire(
                    "UserPromptSubmit",
                    {"prompt": user_query},
                )
                if ups.blocked or ups.stop:
                    reason = ups.block_reason or "本次输入被 hooks 配置拦截。"
                    logger.info("UserPromptSubmit hook 拦截本次输入: reason=%s", reason)
                    self.event_bus.emit(Done(
                        final_answer=reason,
                        rounds_used=0,
                        cancelled=False,
                    ))
                    return reason
                if ups.additional_context:
                    hook_extra_context += ("\n" if hook_extra_context else "") + ups.additional_context

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
        # 把 SessionStart / UserPromptSubmit hook 注入的上下文追加到运行时指令末尾，
        # 让模型在本轮 system 补充段看到 hook 提供的额外信息。
        if hook_extra_context:
            system_instructions = (
                f"{system_instructions}\n\n[hooks 注入上下文]\n{hook_extra_context}"
                if system_instructions else
                f"[hooks 注入上下文]\n{hook_extra_context}"
            )
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
            # blocking_limit 命中:autocompact 已经无法继续释放空间,本轮拒绝
            # 进入 _tool_loop。Error 事件已在 preflight 内 emit,这里再 emit
            # Done 让前端正常关闭本次 chat 渲染,然后返回友好提示文本。
            if preflight.get("blocked"):
                blocked_message = (
                    "[上下文窗口已满] 自动 compact 已无法继续释放空间,"
                    "本轮无法继续。请使用 /clear 或 /compact 后重试。"
                )
                self.event_bus.emit(Done(
                    final_answer=blocked_message,
                    rounds_used=0,
                    cancelled=False,
                    context_window=self.context_window_usage(),
                    auto_compact={
                        "compacted": True,
                        "events": auto_compactions,
                    },
                ))
                return blocked_message
            messages = self._build_chat_messages(
                user_content=request_content,
                system_instructions=system_instructions,
                memory_query=history_user_text,
            )

        # 记录本轮初始消息（含 system/user/history）
        if self.message_logger is not None:
            try:
                self.message_logger.log(
                    messages,
                    tools=tools_schema,
                    label=f"会话开始 | query=\"{history_user_text[:100]}\"",
                )
            except Exception:
                logger.exception("message_logger 写入失败")

        # 记录 tool_loop 开始前 messages 的长度。loop 内会原地累积本轮 assistant
        # (含 tool_calls)/role=tool/最终 assistant，commit 到 history 时只取这之后
        # 新增的部分，避免把 system / state user / 历史轮次重复推回。
        commit_offset = len(messages)

        #工具调用次数，最终回答，工具轨迹，本轮压缩事件
        rounds_used, final_answer, trace_collector, loop_compactions = self._tool_loop(
            messages, tools_schema, token,
        )
        auto_compactions.extend(loop_compactions)

        # CC 模式跨轮累积：把本轮 _tool_loop 内新增的 user/assistant/tool 消息
        # 全部 commit 到 self.history（含 assistant.tool_calls 和 role=tool 的原始
        # 工具结果）。下一轮 _build_chat_messages 会从 history 恢复这些原始块,
        # 模型可以直接看到上一轮真实工具调用细节,不再依赖摘要文本。
        # 注意 history 里第一条仍是用户原始输入的 text 形态(不带多模态 base64),
        # 跨轮 image_url/data URI 不进 history 以免撑爆 token 估算和 transcript。
        history_commit_start = len(self.history)
        self.history.append(Message.create_user_message(history_user_text))
        new_protocol_messages = self._extract_protocol_messages(messages, commit_offset)
        if new_protocol_messages:
            self.history.extend(new_protocol_messages)
        # 兜底:如果工具循环结束时 final_answer 没作为最后一条 assistant 进入
        # messages(例如某些 cancel 路径),手动补一条最终回答,保证下一轮恢复时
        # 仍然能看到本轮的最终输出。
        if final_answer and not self._history_tail_is_final_answer(final_answer):
            self.history.append(Message.create_assistant_message(final_answer))
        committed_turn_messages = list(self.history[history_commit_start:])

        # trace_collector 来自本轮工具循环,只服务 state.json 结构化字段提取
        # (files_seen / files_modified / recent_commands / decisions / pending)。
        # 不再生成 work_record 文本,因为原始工具消息已通过 history 累积传递。
        work_record = self._make_work_record(
            user_query=history_user_text,
            final_answer=final_answer,
            trace_collector=trace_collector,
        )
        # 自动记忆更新:现在只驱动 MEMORY.md 长期记忆(KnowledgeBase.capture_turn
        # 内按用户显式"请记住"类触发写入)。结构化知识页改由模型显式调用
        # knowledge_write 工具写入——原先依赖 work_record 文本的自动知识页捕获
        # 已移除(work_record 文本在 CC 对齐重构后恒为空,且与 knowledge_write
        # 职责重复)。因此这里不再传 work_record_text。
        self._auto_update_memory_and_knowledge(
            user_query=history_user_text,
            final_answer=final_answer,
        )
        self._persist_turn(
            history_user_text,
            final_answer,
            work_record,
            committed_turn_messages,
        )

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

        # Stop hook：整轮回答就绪、Done 收尾前触发（通知类用途，如生成报告/清理）。
        # 第一版不实现 Stop 阻止收尾（防循环逻辑留作后续），只注入可选上下文做记录。
        if self.hook_manager is not None and self.hook_manager.has_event("Stop"):
            self.hook_manager.fire(
                "Stop",
                {"last_assistant_message": final_answer or ""},
                round_idx=rounds_used,
            )

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

        关键:history 部分复用 _sliced_history_dicts(),与真正发给 LLM 的口径
        完全一致——同样经过 boundary 切片 + window 截断 + 孤儿清理,并且序列化
        整条 OpenAI dict(含 assistant.tool_calls 的 arguments 和 role=tool 的
        content)。重构后 history 里大量消息是纯工具调用(content=None),如果像
        旧逻辑那样按"有正文才计入"过滤,会把 file_write/bash 等工具参数整段漏算,
        导致 TUI 的 Context% 系统性偏低;不走切片还会让 /compact 后百分比不降反升。
        """
        parts: List[str] = []
        state_text = self._session_state_text()
        if state_text:
            parts.append("[State]\n" + state_text)

        history_dicts = self._sliced_history_dicts()
        if history_dicts:
            try:
                history_text = json.dumps(history_dicts, ensure_ascii=False, default=str)
            except Exception:
                history_text = str(history_dicts)
            parts.append("[Context]\n" + history_text)
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

    # ---------- 三级阈值常量 ----------

    # 预测性 compact 时,假设本轮还会增长这么多 tokens(LLM 输出 + 一次工具调用
    # 返回)。CC 公式:min(maxOutput, 20k) + 15k。我们没有动态 maxOutput 字段,
    # 直接取保守上限 20k。
    PREDICTIVE_GROWTH_OUTPUT_BUDGET = 20_000
    PREDICTIVE_GROWTH_TOOL_BUDGET = 15_000

    # autocompact 动态 buffer。模型窗口越大,留出的安全余量越多,避免巨大 prompt
    # 在边界附近抖动触发反复压缩。CC 同款分档。
    AUTOCOMPACT_BUFFER_SMALL = 13_000   # 窗口 < 400k
    AUTOCOMPACT_BUFFER_MEDIUM = 30_000  # 400k ≤ 窗口 < 800k
    AUTOCOMPACT_BUFFER_LARGE = 50_000   # 窗口 ≥ 800k

    # blocking 阈值:autocompact 失败/关闭后,窗口剩余 ≤ 这个值就拒绝继续。
    BLOCKING_LIMIT_BUFFER = 3_000

    def _estimate_max_turn_growth(self) -> int:
        """估算本轮请求至此之后还可能增长的 token 数。

        包含两部分:
        - 模型最终输出占用(粗略上限 20k);
        - 一次工具调用返回(粗略上限 15k,涵盖 file_read 等大输出)。

        这个值用于 predictive autocompact:如果当前请求 + 估算增长 > 模型完整
        窗口,就提前触发 compact,而不是等到 API 返回 413 才被动处理。
        """
        return self.PREDICTIVE_GROWTH_OUTPUT_BUDGET + self.PREDICTIVE_GROWTH_TOOL_BUDGET

    def _dynamic_autocompact_buffer(self) -> int:
        """模型窗口对应的 autocompact buffer。"""
        window = self._model_max_tokens()
        if window >= 800_000:
            return self.AUTOCOMPACT_BUFFER_LARGE
        if window >= 400_000:
            return self.AUTOCOMPACT_BUFFER_MEDIUM
        return self.AUTOCOMPACT_BUFFER_SMALL

    def _auto_compact_history(
        self,
        *,
        reason: str,
        round_idx: int = 0,
        force: bool = False,
        request_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """执行一次自动跨轮 compact,在 history 末尾追加 compact_boundary。

        与手动 /compact 走同一条路径(compact_context),只是返回一份审计用的
        轻量事件给 Done.auto_compact 渲染。``force=True`` 用于 preflight:即便
        state/history 单独看没达到 budget,也强制触发。

        compact_boundary 之前的原始消息保留在 history 里(用于审计/恢复),但
        下一次 _build_chat_messages 切片后不再注入到 LLM。
        """
        before_usage = self.context_window_usage()
        budget = int(before_usage["max_tokens"])
        if not force and int(before_usage["used_tokens"]) < budget:
            return None
        before_messages = len(self.history)
        state_text = self._session_state_text()
        if before_messages == 0 and not state_text:
            return None

        # PreCompact hook：在真正执行压缩前触发（已过滤掉 no-op 的早返回路径）。
        # matcher 值归一化：内部各 reason（preflight_*/post_turn）统一暴露为 "auto"，
        # 手动 /compact 暴露为 "manual"，对齐 Claude Code 的 trigger 语义。
        # 第一版仅通知用途（导出/保存上下文），不支持阻止压缩。
        if self.hook_manager is not None and self.hook_manager.has_event("PreCompact"):
            trigger = "manual" if reason == "manual" else "auto"
            self.hook_manager.fire(
                "PreCompact",
                {"trigger": trigger, "reason": reason},
                matcher_value=trigger,
                round_idx=round_idx,
            )

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
        """本轮第一次 think 前的三级阈值检查(对齐 Claude Code)。

        按优先级从严到宽:

        1. **Predictive**:current + estimateMaxTurnGrowth > 模型完整窗口
           触发 autocompact。这里的窗口是真实窗口(model_max_tokens),不是 80%
           的工作窗口——预判逻辑要尽量晚点开火,但不能等到 API 401 才动。
        2. **Autocompact**:current ≥ 模型完整窗口 - dynamic_buffer
           触发 autocompact。dynamic_buffer 按窗口规模分档(13k/30k/50k)。
        3. **Blocking**:autocompact 失败/no-op 后,如果 current ≥ 完整窗口 - 3k
           emit Error 并返回特殊事件,_chat_impl 会据此终止本轮。

        命中前两级会调 _auto_compact_history,在 history 追加 compact_boundary;
        _chat_impl 收到非 None 事件后会重建 messages,boundary 切片让新请求体
        立即变小。
        """
        del user_query, system_instructions  # 估算直接读 messages
        request_tokens = self._estimate_request_tokens(messages, tools_schema)
        full_window = self._model_max_tokens()
        growth = self._estimate_max_turn_growth()
        buffer = self._dynamic_autocompact_buffer()

        # 1. Predictive
        if request_tokens + growth > full_window:
            event = self._auto_compact_history(
                reason="preflight_predictive",
                round_idx=0,
                force=True,
                request_tokens=request_tokens,
            )
            if event is not None:
                event["full_window"] = full_window
                event["growth_estimate"] = growth
                return event

        # 2. Autocompact
        if request_tokens >= full_window - buffer:
            event = self._auto_compact_history(
                reason="preflight_autocompact",
                round_idx=0,
                force=True,
                request_tokens=request_tokens,
            )
            if event is not None:
                event["full_window"] = full_window
                event["buffer_tokens"] = buffer
                return event

        # 3. Blocking
        if request_tokens >= full_window - self.BLOCKING_LIMIT_BUFFER:
            logger.error(
                "blocking limit reached: request=%s window=%s buffer=%s",
                request_tokens, full_window, self.BLOCKING_LIMIT_BUFFER,
            )
            self.event_bus.emit(Error(
                where="session",
                message=(
                    f"上下文窗口即将耗尽 (请求 {request_tokens} tokens >= "
                    f"{full_window - self.BLOCKING_LIMIT_BUFFER}),"
                    "且自动 compact 无法继续释放空间。请手动 /clear 或 /compact 后重试。"
                ),
                round_idx=0,
            ))
            return {
                "reason": "blocking_limit",
                "round_idx": 0,
                "request_tokens": request_tokens,
                "full_window": full_window,
                "blocked": True,
            }

        return None

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
        # trace_collector 仅服务 state.json 结构化字段提取(files_seen 等)。
        # 本轮 messages 在循环结束后会被 _chat_impl 提取协议消息 commit 到
        # self.history,跨轮恢复时模型直接看到原始 tool_calls + tool_result。
        trace_collector = TraceCollector()
        # _tool_loop 内已不再做局部消息压缩。所有跨轮压缩都集中在 preflight
        # 三级阈值 + microcompact + autocompact;loop_compactions 保留为空列表
        # 是为了维持 _chat_impl 的解构调用兼容。
        loop_compactions: List[Dict[str, Any]] = []
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
                        messages,
                        tools=tools_schema,
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
            if self.message_logger is not None:
                try:
                    logged_messages = list(messages)
                    assistant_payload = _llm_result_to_assistant_payload(result)
                    if assistant_payload is not None:
                        logged_messages.append(assistant_payload)
                    self.message_logger.log(
                        logged_messages,
                        tools=tools_schema,
                        response=result,
                        label=f"round {round_idx} after think",
                    )
                except Exception:
                    logger.exception("message_logger write failed")

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
                # 完整工具结果按 OpenAI tool calling 协议回灌给本轮 messages,
                # 同时这一条会在轮末被 _chat_impl 提取并 commit 到 self.history,
                # 下一轮 _build_chat_messages 重新注入,模型继续看到原始结果。
                # result_cap.py 已经在 executor 层对超大输出做过持久化截断,
                # 这里不需要再次压缩。
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
                # trace_collector 用于本轮末尾驱动 state.json 结构化字段更新
                # (files_seen / files_modified / recent_commands 等)。
                trace_collector.add_tool_result(
                    call=call,
                    name=exec_result.name,
                    result=exec_result.result,
                    is_error=exec_result.is_error,
                    round_idx=round_idx,
                )

            if self.message_logger is not None:
                try:
                    self.message_logger.log(
                        messages,
                        tools=tools_schema,
                        label=f"round {round_idx} after tool results",
                    )
                except Exception:
                    logger.exception("message_logger write failed")

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

    def _extract_protocol_messages(
        self,
        messages: List[Dict[str, Any]],
        offset: int,
    ) -> List[Message]:
        """从本轮 _tool_loop 累积的 messages 中抽出新增的协议消息。

        offset 是 _tool_loop 启动前 messages 的长度。从 offset 到末尾的消息里，
        我们只保留可以安全跨轮 commit 的部分：
        - assistant（含 tool_calls / reasoning_content）
        - role=tool（必须有 tool_call_id）
        其它（system / user 等）通常不会出现在 _tool_loop 内部，跳过即可。

        多模态 user 消息不在这里处理：本轮 user_query 由 _chat_impl 直接以
        text 形式 append 到 history，base64 图片不进 history。
        """
        out: List[Message] = []
        for raw in messages[offset:]:
            if not isinstance(raw, dict):
                continue
            role = raw.get("role")
            if role == "assistant":
                content = raw.get("content")
                tool_calls = raw.get("tool_calls")
                # content 为 None 但 tool_calls 非空时也合法（纯工具调用回合）
                text = content if isinstance(content, str) else None
                if not text and not tool_calls:
                    continue
                out.append(Message.create_assistant_message(
                    input_text=text,
                    tool_calls=tool_calls if isinstance(tool_calls, list) else None,
                ))
            elif role == "tool":
                tool_call_id = raw.get("tool_call_id") or ""
                if not tool_call_id:
                    continue
                content = raw.get("content")
                tool_name = raw.get("name") or raw.get("tool_name") or ""
                out.append(Message.create_tool_message(
                    tool_call_id=str(tool_call_id),
                    tool_name=str(tool_name),
                    tool_output=str(content or ""),
                ))
        return out

    def _history_tail_is_final_answer(self, final_answer: str) -> bool:
        """判断 history 末尾是否已经是本轮最终 assistant 回答。

        正常路径下 _tool_loop 最后一条 assistant 消息（无 tool_calls）就是 final
        answer，已经被 _extract_protocol_messages 收进去了；只有 cancel 等异常
        路径才需要兜底再追加一条 assistant。
        """
        if not self.history:
            return False
        last = self.history[-1]
        last_role = last.role.value if hasattr(last.role, "value") else str(last.role)
        if last_role != "assistant":
            return False
        if last.tool_calls:
            return False
        last_content = last.content if isinstance(last.content, str) else ""
        return last_content == final_answer

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

    def _persist_turn(
        self,
        user_query: str,
        final_answer: str,
        work_record,
        committed_messages: List[Message],
    ) -> None:
        """把本轮对话和工作记录写入项目级 session store。

        committed_messages 是本轮 _tool_loop 实际进入 self.history 的消息序列
        (含 user / assistant 含 tool_calls / role=tool / 最终 assistant)。
        透传给 LocalSessionStore.append_turn 用于 transcript 落盘,跨进程恢复
        时模型仍能看到原始工具调用细节。
        """
        if self.session_store is None:
            return
        try:
            self.session_store.append_turn(
                user_query=user_query,
                final_answer=final_answer,
                committed_messages=committed_messages,
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
