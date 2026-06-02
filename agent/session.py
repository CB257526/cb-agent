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

不在这里：
- 启动期 _section/_info：装配阶段的输出，仍由 run_agent.py 主入口打
- /xxx 斜杠命令：CLI 专属功能，REPL 那边处理
- 渲染逻辑（颜色 / 面板）：CLIRenderer 那边

ContextBuilder / ToolRegistry / Executor / LLM 都从外部传入，便于测试和换前端。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

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
from agent.question_registry import QuestionRegistry
from context import ContextBuilder, ContextPacket, ContextPriority
from core.message import Message
from skills.skill_manager import SkillManager
from tools.toolRegistry import ToolRegistry
from agent.work_context import (
    LocalSessionStore,
    RuleTraceSummarizer,
    TraceCollector,
    TraceSummarizer,
    make_work_record_message,
)

logger = logging.getLogger(__name__)


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


class AgentSession:
    """单个 agent 会话。一个进程里通常只有一个，但多会话场景也支持。

    构造时把所有依赖注入进来；运行时只暴露 chat() 一个入口。
    """

    # 工具调用循环最大轮数，防死循环
    MAX_TOOL_ROUNDS = 20

    def __init__(
        self,
        llm: CbAgentsLLM,
        registry: ToolRegistry,
        executor: ToolExecutor,
        event_bus: EventBus,
        builder: Optional[ContextBuilder] = None,
        skill_manager: Optional[SkillManager] = None,
        bash_prompt_provider=None,
        ctx_enabled: bool = True,
        history_window: int = 12,
        messages_snapshot_hook=None,
        session_store: Optional[LocalSessionStore] = None,
        trace_summarizer: Optional[TraceSummarizer] = None,
    ) -> None:
        """
        Args:
            messages_snapshot_hook: 可选回调 (messages, round_idx) -> None，
                每轮 think 前调用一次。给 CLI dump 调试用，不属于事件流（事件
                是结构化的；dump 是面向开发者的"看原始上下文"调试通道）。
        """
        self.llm = llm
        self.registry = registry
        self.executor = executor
        self.event_bus = event_bus
        self.builder = builder
        self.skill_manager = skill_manager
        self.bash_prompt_provider = bash_prompt_provider
        self.ctx_enabled = ctx_enabled
        self.history_window = history_window
        self.messages_snapshot_hook = messages_snapshot_hook
        self.session_store = session_store
        self.trace_summarizer = trace_summarizer
        self.rule_trace_summarizer = RuleTraceSummarizer()
        self.history: List[Message] = []
        if self.session_store is not None:
            try:
                # 启动自动恢复最近会话：这里只恢复普通 user/assistant 消息和
                # 【工作记录】assistant 消息，不恢复 role=tool，也不恢复
                # assistant.tool_calls。tool 协议消息只在同一轮 tool loop 内合法，
                # 跨进程/跨轮直接回灌会造成 tool_call_id 对不上的协议风险。
                self.history = self.session_store.load_latest_history(
                    max_messages=self.history_window,
                )
            except Exception:
                logger.exception("本地会话历史恢复失败，忽略")
                self.history = []
        # 当前正在跑的 chat 的 cancel token；REPL 收 Ctrl-C 时调它的 .cancel()
        # 没在 chat 中时为 None
        self.current_cancel_token: Optional[CancelToken] = None
        # AskUserQuestionTool 用：工具线程 register+wait，gateway 在 RPC 里
        # submit_answer。整个进程一份，session 持有给 gateway/tool 共享。
        self.question_registry: QuestionRegistry = QuestionRegistry()

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
        }

    def create_session(self) -> Dict[str, Any]:
        """创建并切换到一个全新的空会话。

        新会话的隔离语义是：磁盘 active 指针切到新目录，同时内存 history 清空。
        后续 chat 会写入新目录，不会继续追加旧 transcript。
        """
        self.history.clear()
        if self.session_store is None:
            return {"session": None, "history": []}
        summary = self.session_store.create_session()
        return {"session": summary, "history": []}

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
        }

    def chat(
        self,
        user_query: str,
        cancel_token: Optional[CancelToken] = None,
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
            return self._chat_impl(user_query, token)
        finally:
            reset_current_cancel_token(ctx_token)
            self.current_cancel_token = None

    async def chat_async(
        self,
        user_query: str,
        cancel_token: Optional[CancelToken] = None,
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
        return await asyncio.to_thread(self.chat, user_query, cancel_token)

    def _chat_impl(self, user_query: str, token: CancelToken) -> str:
        # 后台任务完成通知 → 注入 user_query 前缀 + 发 BackgroundNotification 事件
        user_query = self._prepend_background_notifications(user_query)

        system_instructions = self._build_system_instructions()
        # 本地 state 是"长期工作态"，比如已读文件、已改文件、最近命令。
        # 它比普通 history 更像任务状态，所以在 ContextBuilder 开启时作为
        # P1_STATE 进入 [State]；关闭 ContextBuilder 时则手工拼入 system。
        state_packet = self._build_state_packet()

        # 构 messages
        if self.ctx_enabled and self.builder is not None:
            messages = self.builder.to_messages(
                user_query=user_query,
                conversation_history=self.history,
                system_instructions=system_instructions,
                additional_packets=[state_packet] if state_packet else None,
            )
        else:
            if state_packet is not None:
                system_instructions = (
                    system_instructions
                    + "\n\n[State]\n关键进展与未决问题：\n"
                    + state_packet.content
                )
            messages = [{"role": "system", "content": system_instructions}]
            for m in self.history[-self.history_window:]:
                messages.append(m.to_dict())
            messages.append({"role": "user", "content": user_query})

        tools_schema = (
            self.registry.get_tools_description_openai_schema()
            if self.llm.is_Function_Calling
            else None
        )

        rounds_used, final_answer, trace_collector = self._tool_loop(
            messages, tools_schema, token,
        )

        # 跨轮历史仍然先保存用户输入和最终回答，保持原来的对话语义。
        # 下面的 work_record 是额外的普通 assistant 文本，不是 tool 消息。
        self.history.append(Message.create_user_message(user_query))
        if final_answer:
            self.history.append(Message.create_assistant_message(final_answer))
        # trace_collector 来自本轮工具循环，里面只有被压缩过的工具事实。
        # 如果本轮没有工具调用，就不会生成【工作记录】，避免无意义地撑大 history。
        work_record = self._make_work_record(
            user_query=user_query,
            final_answer=final_answer,
            trace_collector=trace_collector,
        )
        if work_record is not None and work_record.text:
            self.history.append(make_work_record_message(work_record))
        self._persist_turn(user_query, final_answer, work_record)

        # Done 事件：让前端知道整轮结束
        self.event_bus.emit(Done(
            final_answer=final_answer,
            rounds_used=rounds_used,
            cancelled=token.is_cancelled(),
        ))
        return final_answer

    def clear_history(self) -> None:
        # /clear 的新语义是"彻底清理当前会话"：内存 history 和项目级
        # .cbagent/sessions active session 都删掉。下一轮继续聊天时 store 会
        # 自动创建新 session，不会把旧上下文再恢复回来。
        self.history.clear()
        if self.session_store is not None:
            try:
                self.session_store.clear_active_session()
            except Exception:
                logger.exception("清理本地会话失败")

    # ---------- 工具循环 ----------

    def _tool_loop(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
        token: CancelToken,
    ) -> tuple[int, str, TraceCollector]:
        """工具调用主循环。返回 (rounds_used, final_answer, trace_collector)。

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
                )

            self.event_bus.emit(RoundStart(
                round_idx=round_idx,
                max_rounds=self.MAX_TOOL_ROUNDS,
            ))
            if self.messages_snapshot_hook is not None:
                try:
                    self.messages_snapshot_hook(messages, round_idx)
                except Exception:
                    logger.exception("messages_snapshot_hook 抛异常，已吞")

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
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, final, trace_collector

            if not isinstance(result, dict):
                self.event_bus.emit(Error(
                    where="llm",
                    message=f"模型返回非预期结构: {type(result).__name__}",
                    round_idx=round_idx,
                ))
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, "", trace_collector

            answer = result.get("answer", "") or ""
            tool_calls = result.get("tool_calls") or []
            reasoning = result.get("reasoning_content")
            # 流式中途被 cancel：cb_agents 已 emit Cancelled，answer 是已收的部分
            if answer:
                partial_answer = answer

            # 流式过程中被 cancel → 不再发起新一轮工具调用，直接收尾
            if token.is_cancelled():
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, answer, trace_collector

            if not tool_calls:
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, answer, trace_collector

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

            # 调度执行（事件由 ToolExecutor 自己 emit ToolStart/ToolComplete）
            # token 透传给 executor：串行/并发模式下都在工具间做 cancel 检查
            results = self.executor.execute(
                tool_calls, round_idx=round_idx, cancel_token=token,
            )
            for call, exec_result in zip(tool_calls, results):
                # 完整工具结果仍按 OpenAI tool calling 协议回灌给本轮 messages。
                # 这一点不能改，否则多轮工具调用时模型看不到真实工具输出。
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
                trace_collector.add_tool_result(
                    call=call,
                    name=exec_result.name,
                    result=exec_result.result,
                    is_error=exec_result.is_error,
                    round_idx=round_idx,
                )

            self.event_bus.emit(RoundEnd(
                round_idx=round_idx, has_tool_calls=True, final=False,
            ))

        # 超出最大轮数
        self.event_bus.emit(Error(
            where="session",
            message=f"工具调用超过 {self.MAX_TOOL_ROUNDS} 轮，强制终止",
            round_idx=self.MAX_TOOL_ROUNDS,
        ))
        return self.MAX_TOOL_ROUNDS, "（工具调用次数过多，已终止本轮）", trace_collector

    # ---------- 辅助 ----------

    def _build_state_packet(self) -> Optional[ContextPacket]:
        """把本地滚动状态转换成 ContextBuilder 可消费的高优先级 packet。"""
        if self.session_store is None:
            return None
        try:
            text = self.session_store.state_text()
        except Exception:
            logger.exception("本地会话状态读取失败")
            return None
        if not text:
            return None
        return ContextPacket(
            content=text,
            priority=ContextPriority.P1_STATE,
            metadata={"source": "local_session_state"},
        )

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
        """组装 system prompt：角色 + 工具清单 + Bash prompt + Skill 概览。

        从 ToolRegistry 动态拉工具描述，避免和实际注册脱节。
        """
        parts = [
            "你是 cb-agent 的智能助手。下面列出当前可用的能力，按需调用：",
            "遇到复杂的问题是请务必调用todo工具",
            "",
        ]

        tools_desc = self.registry.get_tools_description()
        if tools_desc and tools_desc != "暂无可用工具":
            parts.append(tools_desc)
        else:
            parts.append("（当前没有已注册的工具）")

        parts.extend([
            "",
            "调用工具时选最直接的那个，避免连续多轮无意义调用。",
            "回答用中文，简明扼要。",
        ])

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
