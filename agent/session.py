"""AgentSession：纯逻辑会话核心，无 print。

Stage 3 拆出来的"中间层"。它把 Stage 1+2 的 EventBus / ToolExecutor 跟原来
AgentRunner 的会话主流程组合起来，但**不直接做任何输出**——所有"现在发生了什么"
都经 EventBus 派发，留给 OTUI、通讯平台或其他前端订阅渲染。

跟原 AgentRunner 的差别：
- _chat_once → chat()：返回 final_answer，让调用方决定怎么展示
- _tool_loop：继续在这里，但每轮 think 传 event_bus，工具循环的 RoundStart /
  RoundEnd / Error / Done 也都经 bus 而非 print
- _build_system_instructions / _prepend_background_notifications：纯字符串
  组装，跟 print 无关，原样搬过来
- 历史管理（self.history）也在 session 里（前端无需知道历史结构）

不在这里:
- 启动期 _section/_info：装配阶段的输出，仍由 run_agent.py 主入口打
- /xxx 斜杠命令：由具体前端处理
- 渲染逻辑（颜色 / 面板）：由具体前端处理

上下文工程模块对接：
- ConversationHistory 是唯一的模型可见内存历史；普通运行只追加，正式 compact
  才允许按 generation 事务替换。
- 每次 provider 请求只临时组装“稳定 system 外壳 + canonical history”，不会在
  组装阶段裁剪、重排、修补或改写历史。
- history.jsonl 是唯一恢复事实源；旧 transcript/compact/active 文件只在首次
  迁移时读取，正常运行不再维护双份状态。
- memory_loader 在 run_agent.py 装配；work_context.py 只保留现场状态、usage、
  token 校准和工具轨迹索引，不再负责对话历史。

ToolRegistry / Executor / LLM 仍从外部传入,便于测试和换前端。
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from agent.cancel import (
    CancelToken,
    set_current_cancel_token,
    reset_current_cancel_token,
)
from subagent.context import (
    reset_current_parent_session_id,
    set_current_parent_session_id,
)
from tools.tools.pending_images import (
    reset_pending_image_buffer,
    set_pending_image_buffer,
)
from agent.cb_agents import CbAgentsLLM
from agent.compaction import (
    CompactionError,
    dynamic_retained_token_target,
    estimate_message_tokens,
    make_summary_message,
    partition_history_for_compaction,
    run_local_compaction,
)
from agent.llm_errors import (
    LLMContextOverflowError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTransportError,
)
from agent.event_bus import EventBus
from agent.events import (
    BackgroundNotification, Cancelled, ContextWindowUpdated, Done, Error, PlanApproved, PlanDelta,
    PlanModeChanged, PlanReady, PlanRejected, PlanStart, RoundEnd, RoundStart, TextDelta, TokenUsage,
)
from agent.executor import ToolExecutor
from agent.message_logger import MessageLogger
from agent.multimodal_input import process_multimodal_prompt, sanitize_multimodal_payload
from agent.plan_parser import PlanSegment, ProposedPlanParser, split_proposed_plan_text
from agent.plan_policy import PLAN_READ_ACTIONS, PLAN_READ_TOOLS, PlanExecutionPolicy
from agent.plan_state import PlanStateStore
from agent.question_registry import QuestionRegistry
from constant.llm.constant_llm import ConstantLLM
from context import (
    MemoryLoader,
    count_tokens,
    get_dynamic_context_sections,
    get_static_system_prompt,
)
from context.world_state import DynamicSectionResult, EMPTY_WORLD_STATE, WorldStateSnapshot
from core.conversation_history import ConversationHistory
from core.message import Message, MessageRole
from skills.skill_manager import SkillManager
from tools.toolRegistry import ToolRegistry
from agent.message_protocol import validate_tool_protocol
from agent.work_context import (
    LocalSessionStore,
    TraceStateIndexer,
    TraceCollector,
)
from agent.history_journal import HistoryJournal
from agent.legacy_history_migrator import load_legacy_history
logger = logging.getLogger(__name__)

# metadata.kind 标记运行时上下文更新消息。这类消息不在 UI 中展示；compact 摘要
# 请求仍会看到其结构化原文，但 replacement 的原始回合不重复保留，现场连续性由
# world state snapshot 单独负责。
CONTEXT_UPDATE_KIND = "context_update" #标记一个user类型的消息是否属于section块更新的消息
WORLD_STATE_SNAPSHOT_KEY = "world_state_snapshot"
CONTEXT_EVIDENCE_KIND = "context_evidence"
# 区分“调用方未提供请求外壳”和“本轮外壳明确为空”。后者也必须被冻结，不能
# 在下一工具轮重新读取动态注册表。
_REQUEST_SNAPSHOT_UNSET = object()


@dataclass(frozen=True)
class PreparedTurnInput:
    """一次用户回合在写入唯一 history 前的不可变候选批次。"""

    messages: tuple[Message, ...]
    world_state: WorldStateSnapshot


def _clip_preview_text(text: Any, limit: int = 1200) -> str:
    """把 UI 与状态预览文本裁到固定字符数。"""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = " ".join(s.split())
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "…"


def _world_state_from_history(history: Sequence[Message]) -> WorldStateSnapshot:
    """从最近一次 context update 恢复模型已见的完整现场基线。"""
    for message in reversed(history):
        if _message_kind(message) != CONTEXT_UPDATE_KIND:
            continue
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        return WorldStateSnapshot.from_payload(metadata.get(WORLD_STATE_SNAPSHOT_KEY))
    return EMPTY_WORLD_STATE


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

    跨会话切换恢复的是"用户看到的对话记录 + 工作记录文本"，不是工具调用协议，
    因此不导出 assistant.tool_calls。tool 角色会压成短摘要，避免 UI 首屏直接
    展示大段 stdout/stderr；模型上下文仍然使用内存里的原始 Message。
    """
    role = message.role.value if hasattr(message.role, "value") else str(message.role)
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    payload: Dict[str, Any] = {
        "role": role,
        "content": _message_content_to_text(message.content),
        "kind": metadata.get("kind"),
    }
    if metadata.get("interrupted"):
        payload["interrupted"] = True
    if role == "tool":
        tool_name = str(message.tool_name or "")
        call_id = str(message.tool_call_id or "")
        is_error = bool(message.is_error)
        preview = _clip_preview_text(_message_content_to_text(message.content), 240)
        label = tool_name or call_id or "tool"
        status_label = "工具失败" if is_error else "工具完成"
        payload["content"] = f"【{status_label}】{label}" + (f": {preview}" if preview else "")
        payload["tool"] = {
            "name": tool_name,
            "call_id": call_id,
            "is_error": is_error,
        }
    return payload


def _message_kind(message: Message) -> str:
    """读取本地 message kind。

    context update/compaction summary 在 OpenAI 协议里都是普通消息，
    本地只靠 metadata.kind 区分用途。compact 保留最近回合时要排除这类
    维护性消息，只留下真正的 user/assistant 对话。
    """
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("kind") or "")


def _format_context_update_text(context_text: str) -> str:
    """把运行时上下文包装为低优先级的 Chat user 消息。

    设计意图 —— 与 provider 端 prompt cache 对齐:
    Chat Completions 协议里只有首条 system message 的字节序列是稳定可缓存的。
    运行时上下文(env_info / CLAUDE.md memory / MCP 指令 / 技能列表 / 时间戳等)
    每轮都可能变化,如果混进 system message 会导致整个前缀变掉 → 缓存失效。

    解决方案: 把变动的上下文作为独立的 user 消息放到请求尾部附近,并用
    <context-update> 标签标明其"信息性/低优先级"语义。这条消息虽然 role=user,
    但模型被系统指令告知它的权重低于用户的直接指令。

    为什么不用 role=system: OpenAI 协议只支持单条或多条 system message,且
    第二、三条 system message 在多数 provider 上的行为未定义(有些静默丢弃,
    有些合并,有些报 400)。
    """
    return (
        "<context-update>\n"
        "The following runtime context is informational and lower priority "
        "than the system message and the user's direct request.\n\n"
        + context_text
        + "\n</context-update>"
    )


def _format_context_sections(
    changed_sections: Sequence[tuple[str, str]],
    removed_sections: Sequence[str],
) -> str:
    """把变化块渲染成具名更新，明确新值替换同名旧值。
       接受改变了的section块与要删除的section块
       返回一个类似于
       section_name:env macos
       section_name:env state="removed"
       意思env块更新为macos，之前的env块已被删除
    """
    parts = [
        "The newest section with the same name replaces earlier values.",
    ]
    for name, text in changed_sections:
        parts.append(
            f'<context-section name="{name}">\n{text.strip()}\n</context-section>'
        )
    for name in removed_sections:
        parts.append(f'<context-section name="{name}" state="removed" />')
    return "\n\n".join(parts)


def _make_context_update_message(
    context_text: str,
    world_state_snapshot: Optional[WorldStateSnapshot] = None,
) -> Message:
    """基于运行时上下文文本构造 Message 对象。

    标记 metadata.kind = CONTEXT_UPDATE_KIND,用于:
    - export_history() 中过滤掉(UI 不展示上下文更新消息)
    - compact 的原始回合保留阶段跳过，现场信息由 world state 单独管理
    - 跨轮恢复时恢复完整 baseline，避免重启后重复注入全部现场
    """
    metadata: Dict[str, Any] = {"kind": CONTEXT_UPDATE_KIND}
    if world_state_snapshot is not None:
        metadata[WORLD_STATE_SNAPSHOT_KEY] = world_state_snapshot.to_payload()
    return Message(
        role=MessageRole.USER,
        content=_format_context_update_text(context_text),
        metadata=metadata,
    )


def _make_context_evidence_message(name: str, text: str) -> Message:
    """构造会进入 history、但不参与 world-state baseline 的回合证据。"""

    key = str(name or "context").strip() or "context"
    body = str(text or "").strip()
    return Message(
        role=MessageRole.USER,
        content=(
            f'<context-evidence name="{key}">\n'
            f"{body}\n"
            "</context-evidence>"
        ),
        metadata={"kind": CONTEXT_EVIDENCE_KIND, "section_name": key},
    )


def _llm_result_to_assistant_payload(result: Any) -> Optional[Dict[str, Any]]:
    """把 LLM 结果转换成 assistant 角色的日志载荷。"""
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


class _PlanParsingEventBus:
    """EventBus 代理，将流式文本路由为 normal TextDelta 或 PlanDelta 事件。

    这是 Plan Mode 流式解析的关键组件。Plan Mode 下，LLM 的 think() 调用
    传入这个 facade 而非真实的 self.event_bus。内部用 ProposedPlanParser
    实时检测 <proposed_plan> 块边界：

    - 块外的文本 → 继续发 TextDelta（用户看到正常回答）
    - 块内的文本 → 发 PlanDelta（前端渲染到独立的计划面板）
    - 检测到块结束 → finish() 保存 pending plan 并 emit PlanReady

    为什么用 facade 而非在 cb_agents 层解析：
    cb_agents 只负责 OpenAI 协议适配（chunk 重组），不应该感知业务语义。
    计划块解析是 session 层的协作模式逻辑，通过替换 event_bus 实现零侵入。
    """

    def __init__(self, session: "AgentSession") -> None:
        self.session = session
        self.parser = ProposedPlanParser()
        self.visible_accumulated = ""
        self.plan_accumulated = ""
        self.latest_plan_text: Optional[str] = None
        self.saved_plan_text: Optional[str] = None

    def emit(self, event: Any) -> None:
        if not isinstance(event, TextDelta):
            self.session.event_bus.emit(event)
            return

        for segment in self.parser.push(event.delta):
            self._emit_segment(segment, event.round_idx)

    def finish(self, round_idx: int) -> None:
        for segment in self.parser.finish():
            self._emit_segment(segment, round_idx)
        plan = (self.latest_plan_text or "").strip()
        if plan:
            self.session._save_pending_plan(plan, round_idx=round_idx)
            self.saved_plan_text = plan

    def _emit_segment(self, segment: PlanSegment, round_idx: int) -> None:
        if segment.kind == "normal":
            if not segment.text:
                return
            self.visible_accumulated += segment.text
            self.session.event_bus.emit(TextDelta(
                delta=segment.text,
                accumulated=self.visible_accumulated,
                round_idx=round_idx,
            ))
            return
        if segment.kind == "plan_start":
            self.plan_accumulated = ""
            self.session.event_bus.emit(PlanStart(round_idx=round_idx))
            return
        if segment.kind == "plan_delta":
            if not segment.text:
                return
            self.plan_accumulated += segment.text
            self.session.event_bus.emit(PlanDelta(
                delta=segment.text,
                accumulated=self.plan_accumulated,
                round_idx=round_idx,
            ))
            return
        if segment.kind == "plan_end":
            plan = self.plan_accumulated.strip()
            if plan:
                self.latest_plan_text = plan
            self.plan_accumulated = ""


class AgentSession:
    """单个 agent 会话。一个进程里通常只有一个，但多会话场景也支持。

    构造时把所有依赖注入进来；运行时只暴露 chat() 一个入口。
    """

    # 工具调用循环最大轮数，防死循环
    MAX_TOOL_ROUNDS = 400

    @property
    def history(self) -> ConversationHistory:
        """返回当前会话唯一的模型可见历史。"""

        return self._history

    @history.setter
    def history(self, value: Any) -> None:
        """兼容旧装配代码传入消息序列，但统一转换为 canonical 容器。"""

        self._history = (
            value
            if isinstance(value, ConversationHistory)
            else ConversationHistory(list(value or []))
        )

    def __init__(
        self,
        llm: CbAgentsLLM,
        registry: ToolRegistry,
        executor: ToolExecutor,
        event_bus: EventBus,
        memory_loader: Optional[MemoryLoader] = None,
        skill_manager: Optional[SkillManager] = None,
        bash_prompt_provider=None,
        ctx_enabled: bool = True,  # 控制动态上下文 section 是否启用
        session_store: Optional[LocalSessionStore] = None,
        message_logger: Optional[MessageLogger] = None,
        language: Optional[str] = "Chinese",
        mcp_clients=None,
        hook_manager: Optional[Any] = None,
        system_prompt_addendum: Optional[str] = None,
        max_tool_rounds: Optional[int] = None,
        memory_writeback_enabled: bool = True, #控制是否在每轮对话结束后自动更新长期记忆（MEMORY.md 文件）
        is_subagent: bool = False,
        subagent_task_registry: Optional[Any] = None,
        runtime_session_id: Optional[str] = None,
        tool_execution_policy: Optional[Any] = None,
        runtime_message_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        """
        Args:
            tool_execution_policy: 工具执行策略。默认 None,按顺序执行。
            system_prompt_addendum: 可选系统提示词补充。用于调整模型行为。
            message_logger: 可选消息日志记录器。非 None 时,在每次 LLM 调用前后
                将完整 messages 列表写入独立日志文件,包含所有 role 的消息全文。
            memory_loader: 多级 Markdown/CLAUDE.md 加载器。为 None 时动态
                context 不注入 memory section,适合 --bare 模式。
        """
        self.llm = llm
        self.registry = registry
        self.executor = executor
        self.event_bus = event_bus
        self.memory_loader = memory_loader
        self.skill_manager = skill_manager
        self.bash_prompt_provider = bash_prompt_provider
        self.ctx_enabled = ctx_enabled
        # active history 始终全量发送；超窗只通过正式 compact 释放，禁止按消息数裁剪。
        self.session_store = session_store
        # provider usage 到达时用 round_idx 找回同一请求的原始估算，既用于 Context
        # 精确刷新，也用于按 provider/model 校准本地 tokenizer 的系统性偏差。
        self._request_token_estimates: Dict[int, int] = {}
        self._token_calibration: Dict[str, float] = {}
        self._calibration_samples: Dict[str, int] = {}
        if not is_subagent:
            self.event_bus.subscribe(self._on_token_usage, TokenUsage)
        self.message_logger = message_logger
        self.language = language
        self.mcp_clients = mcp_clients
        self.system_prompt_addendum = system_prompt_addendum or ""
        self.max_tool_rounds = int(max_tool_rounds or self.MAX_TOOL_ROUNDS)
        self.memory_writeback_enabled = memory_writeback_enabled
        self.is_subagent = is_subagent
        self.subagent_task_registry = subagent_task_registry
        # 无持久化会话（群聊、子代理）也需要稳定所有者 ID，供后台任务隔离使用。
        self.runtime_session_id = runtime_session_id or f"runtime-{uuid.uuid4().hex[:12]}"
        # 子代理角色权限等固定执行策略。Plan Mode 策略仍由主会话动态计算。
        self.tool_execution_policy = tool_execution_policy
        # 运行中补充消息提供器，子代理在每个模型轮次前从任务邮箱取新指令。
        self.runtime_message_provider = runtime_message_provider
        # 可选 HookManager：在用户提交、会话开始、上下文压缩、收尾等生命周期点
        # 触发用户可配置的 hook。None 表示不启用 hooks（零回归）。
        self.hook_manager = hook_manager
        # SessionStart 只在「本会话首个 Prompt」触发一次，这个标志做去重。
        self._session_start_fired = False
        self.trace_state_indexer = TraceStateIndexer()
        self.history = ConversationHistory()
        self._history_journal: Optional[HistoryJournal] = None
        self.plan_store = PlanStateStore(session_store=self.session_store)
        if self.session_store is not None:
            self._history_journal = HistoryJournal(lambda: self.session_store.active_dir)
            try:
                # 新格式只回放 canonical journal。旧 transcript/active/pending 仅在
                # history.jsonl 不存在时由迁移入口读取一次，正常请求不再双读。
                recovery = self._history_journal.recover(
                    legacy_loader=lambda: load_legacy_history(
                        self.session_store.active_dir
                    ),
                )
                self.history = recovery.history
                if recovery.warnings:
                    logger.warning("canonical history 恢复警告: %s", recovery.warnings)
            except Exception as error:
                logger.exception("本地会话 canonical history 恢复失败")
                raise RuntimeError(
                    "本地会话历史恢复失败，已阻止启动以避免静默丢失上下文"
                ) from error
        # 从最近一条 context update 恢复模型实际看过的 section 值。缺少新格式快照
        # 时按空基线处理，下一轮会完整注入一次。
        self._world_state_baseline = _world_state_from_history(self.history)
        # 当前正在跑的 chat 的 cancel token；前端取消 RPC 会调用它的 .cancel()
        # 没在 chat 中时为 None
        self.current_cancel_token: Optional[CancelToken] = None
        # AskUserQuestionTool 用:工具线程 register+wait,gateway 在 RPC 里
        # submit_answer。整个进程一份,session 持有给 gateway/tool 共享。
        self.question_registry: QuestionRegistry = QuestionRegistry()
        # MCP 后台加载由 AgentRunner 装配,但 Gateway 只持有 AgentSession。
        # 因此这里暴露两个可选回调槽位:
        # - mcp_status_provider:只读当前连接快照;
        # - mcp_background_loader:幂等启动后台连接并返回快照。
        # 这两个状态只服务前端展示，不写入 history，也不参与 system prompt。
        self.mcp_status_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self.mcp_background_loader: Optional[Callable[[], Dict[str, Any]]] = None
        logger.info(
            "AgentSession initialized: ctx_enabled=%s restored_history=%s message_logger=%s",
            self.ctx_enabled,
            len(self.history),
            bool(self.message_logger),
        )

    def _append_history(
        self,
        messages: Sequence[Message],
        *,
        turn_id: str = "",
        event_kind: str = "append",
    ) -> List[Message]:
        """把模型可见消息先写 journal，再推进唯一内存 history。"""

        if not messages:
            return []
        if self._history_journal is not None:
            # /clear 后首次模式切换或 chat 也可能先于其它 store 写入发生。
            # journal 是模型历史的事务起点，因此它自己必须确保会话目录已创建。
            if self.session_store is not None:
                self.session_store.ensure_active()
            return self._history_journal.append(
                self.history,
                messages,
                turn_id=turn_id,
                event_kind=event_kind,
            )
        return self.history.append_batch(messages, turn_id=turn_id)

    def _replace_history(
        self,
        messages: Sequence[Message],
        *,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> List[Message]:
        """事务安装正式 compact replacement。"""

        if self._history_journal is not None:
            if self.session_store is not None:
                self.session_store.ensure_active()
            return self._history_journal.replace(
                self.history,
                messages,
                reason=reason,
                metadata=metadata,
            )
        prepared = self.history.prepare_batch(messages)
        self.history.replace_prepared(
            prepared,
            generation=self.history.generation + 1,
        )
        return prepared

    # ---------- 公共入口 ----------

    def export_history(self) -> List[Dict[str, Any]]:
        """导出当前内存 history，供 RPC/TUI 在切换会话后重绘屏幕。

        这不是给 LLM 的上下文构造函数；provider 请求始终直接派生自唯一 history。
        导出层只服务 UI，因此会丢弃部分协议字段并保留可渲染文本。
        """
        return [
            _history_message_to_payload(m)
            for m in self.history
            if _message_kind(m) not in {
                CONTEXT_UPDATE_KIND,
                CONTEXT_EVIDENCE_KIND,
                "plan_state",
                "tool_image_bridge",
                "turn_aborted",
            }
        ]

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
            "usage": self._session_usage_payload(),
            "plan_state": self.plan_state(),
            "subagent_tasks": self._subagent_tasks_payload(),
        }

    def current_runtime_session_id(self) -> str:
        """返回当前会话用于后台任务所有权校验的稳定 ID。"""

        if self.session_store is not None:
            # /clear 后 active_session_id 为空。必须在进入新一轮工具调用前创建新
            # 会话，否则这一轮启动的后台任务会错误归到 runtime 兜底 ID。
            ensure_active = getattr(self.session_store, "ensure_active", None)
            if callable(ensure_active):
                ensure_active()
            active = getattr(self.session_store, "active_session_id", None)
            if active:
                return str(active)
        return self.runtime_session_id

    def _subagent_tasks_payload(self) -> List[Dict[str, Any]]:
        """返回当前会话的活动任务和最近完成任务，供 UI 切换后恢复面板。"""

        manager = self.subagent_task_registry
        if manager is None:
            return []
        try:
            tasks = manager.list(self.current_runtime_session_id())
        except Exception:
            logger.exception("读取当前会话子代理任务失败")
            return []
        active = [task for task in tasks if not task.is_terminal()]
        terminal = [task for task in tasks if task.is_terminal()][-10:]
        selected = active + terminal
        return [task.to_dict() for task in selected]

    def _session_usage_payload(self) -> Dict[str, Any]:
        """返回当前主会话累计 Usage；无持久化 store 时返回进程内零值。"""
        if self.session_store is None:
            return {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cached_prompt_tokens": 0,
                "cache_miss_tokens": 0,
                "requests": 0,
            }
        return self.session_store.load_usage()

    def _calibration_key(self) -> str:
        """生成 provider/model 级校准键，避免切换中转站后沿用错误比例。"""
        base_url = str(getattr(self.llm, "base_url", "") or "")
        model = str(getattr(self.llm, "model", "") or "")
        return f"{base_url}|{model}"

    def _calibration_ratio(self) -> float:
        """读取当前 provider/model 的估算校准系数。"""
        key = self._calibration_key()
        if key not in self._token_calibration and self.session_store is not None:
            stored = self.session_store.load_token_calibration(key)
            if stored is not None:
                self._token_calibration[key] = min(1.25, max(0.75, stored))
        return self._token_calibration.get(key, 1.0)

    def _calibrated_request_tokens(self, raw_tokens: int) -> int:
        """用有界校准系数修正本地 tokenizer 的系统性偏差。"""
        return max(0, int(math.ceil(max(0, raw_tokens) * self._calibration_ratio())))

    def _on_token_usage(self, event: TokenUsage) -> None:
        """持久化单次 Usage，并用 provider 实际输入量校准 Context 估算。"""
        if self.session_store is not None:
            self.session_store.add_token_usage(event)

        raw = self._request_token_estimates.pop(int(event.round_idx or 0), None)
        actual = max(0, int(event.prompt_tokens or 0))
        if raw and actual:
            key = self._calibration_key()
            sample = min(1.25, max(0.75, actual / raw))
            previous = self._calibration_ratio()
            samples = self._calibration_samples.get(key, 0) + 1
            ratio = sample if samples == 1 and key not in self._token_calibration else previous * 0.8 + sample * 0.2
            self._token_calibration[key] = ratio
            self._calibration_samples[key] = samples
            if self.session_store is not None:
                self.session_store.save_token_calibration(key, ratio, samples)

        if actual:
            self.event_bus.emit(ContextWindowUpdated(
                context_window=self._context_window_payload(
                    used_tokens=actual,
                    raw_estimated_tokens=raw if raw is not None else actual,
                    source="provider",
                    scope="current_request",
                ),
                reason="provider_usage",
                round_idx=int(event.round_idx or 0),
            ))

    def plan_state(self) -> Dict[str, Any]:
        """返回当前活跃会话的 Plan Mode 完整状态。

        包含 mode / status / revision / pending_plan / approved_plan /
        last_feedback 等字段。供 Gateway RPC 和 TUI 状态同步使用。
        """
        return self.plan_store.load(include_content=True)

    def collaboration_mode(self) -> str:
        """返回当前协作模式: "execute" 或 "plan"。

        这个值影响:
        - 工具列表过滤（plan 模式只暴露只读工具）
        - 工具 schema 过滤（_filter_tools_schema_for_plan_mode）
        - 工具执行策略（_plan_execution_policy 启用服务端拒绝）
        - 上下文注入（_plan_context_text 注入 Plan Mode 指令）
        - 计划块解析（_PlanParsingEventBus 截获流式输出）
        """
        if self.is_subagent:
            # 子代理权限由角色定义和 SubagentExecutionPolicy 独立控制，不能继承
            # 共享 fallback plan 目录，否则 Worker 会被父会话 Plan Mode 意外降权。
            return "execute"
        mode = str(self.plan_store.load(include_content=False).get("mode") or "execute")
        return mode if mode in {"execute", "plan"} else "execute"

    def _append_plan_state_event(
        self,
        *,
        action: str,
        content: str,
        state: Dict[str, Any],
    ) -> None:
        """把会影响后续模型行为的 Plan 状态变化记录为正式历史证据。"""

        control_turn_id = f"plan-{uuid.uuid4().hex}"
        self._append_history(
            [Message(
                role=MessageRole.USER,
                content=(
                    f'<plan-state action="{action}">\n'
                    f"{str(content or '').strip()}\n"
                    "</plan-state>"
                ),
                metadata={
                    "kind": "plan_state",
                    "action": action,
                    "mode": str(state.get("mode") or ""),
                    "status": str(state.get("status") or ""),
                    "revision": int(state.get("revision") or 0),
                },
            )],
            turn_id=control_turn_id,
            event_kind="plan_state",
        )

    def set_collaboration_mode(self, mode: str) -> Dict[str, Any]:
        """切换协作模式，emit PlanModeChanged 事件通知所有前端。

        mode 必须是 "execute" 或 "plan"。
        返回包含新 mode / plan_state / session 摘要的 payload。
        """
        previous_mode = self.collaboration_mode()
        state = self.plan_store.set_mode(mode)
        if previous_mode != state.get("mode"):
            self._append_plan_state_event(
                action="mode_changed",
                content=f"协作模式从 {previous_mode} 切换为 {state.get('mode', mode)}。",
                state=state,
            )
        self.event_bus.emit(PlanModeChanged(mode=state.get("mode", mode), plan_state=state))
        return {
            "mode": state.get("mode", mode),
            "plan_state": state,
            "session": self.current_session_payload().get("session"),
        }

    def approve_plan(self) -> Dict[str, Any]:
        """批准当前 pending plan，切回 execute 模式。

        current.md → approved.md（复制），status → approved，
        后续 LLM 上下文会注入已批准计划内容作为实施指南。
        emit PlanApproved + PlanModeChanged(execute)。
        如果没有 pending plan 则抛 ValueError。
        """
        state = self.plan_store.approve()
        plan = str(state.get("approved_plan") or "")
        self._append_plan_state_event(
            action="approved",
            content=(
                f"用户已批准计划 revision={state.get('approved_revision')}。\n"
                f"计划文件：{state.get('approved_path') or ''}\n"
                "协作模式已切回 execute。"
            ),
            state=state,
        )
        self.event_bus.emit(PlanApproved(plan=plan, plan_state=state))
        self.event_bus.emit(PlanModeChanged(mode="execute", plan_state=state))
        return {"approved": True, "mode": "execute", "plan": plan, "plan_state": state}

    def reject_plan(self, feedback: str) -> Dict[str, Any]:
        """拒绝当前 pending plan，附修改反馈。

        feedback 持久化到 state.json，下一轮 chat 注入 LLM 上下文
        告知模型"用户拒绝 + 反馈"，LLM 应提交修订后的替代计划。
        mode 保持在 plan，status → rejected。
        emit PlanRejected + PlanModeChanged(plan)。
        """
        state = self.plan_store.reject(feedback)
        self._append_plan_state_event(
            action="rejected",
            content=(
                "用户拒绝了当前计划。\n"
                f"反馈：{str(feedback or '').strip()}"
            ),
            state=state,
        )
        self.event_bus.emit(PlanRejected(feedback=str(feedback or ""), plan_state=state))
        self.event_bus.emit(PlanModeChanged(mode="plan", plan_state=state))
        return {"rejected": True, "mode": "plan", "plan_state": state}

    def create_session(self) -> Dict[str, Any]:
        """创建并切换到一个全新的空会话。

        新会话的隔离语义是：磁盘 active 指针切到新目录，同时内存 history 清空。
        后续 chat 会写入新目录，不会继续追加旧 history journal。
        """
        if self.session_store is None:
            self.history.clear_memory()
            self._world_state_baseline = EMPTY_WORLD_STATE
            return {
                "session": None,
                "history": [],
                "context_window": self.context_window_usage(),
                "usage": self._session_usage_payload(),
                "plan_state": self.plan_state(),
                "subagent_tasks": self._subagent_tasks_payload(),
            }
        # store 完整创建目标目录和 active 索引后，内存 canonical history 才切到空
        # 会话。磁盘失败时旧会话仍可继续使用，不会产生 history/journal 分叉。
        summary = self.session_store.create_session()
        self.history.clear_memory()
        self._world_state_baseline = EMPTY_WORLD_STATE
        if self._history_journal is not None:
            self._history_journal.last_event_seq = 0
        return {
            "session": summary,
            "history": [],
            "context_window": self.context_window_usage(),
            "usage": self._session_usage_payload(),
            "plan_state": self.plan_state(),
            "subagent_tasks": self._subagent_tasks_payload(),
        }

    def switch_session(self, session_id: str) -> Dict[str, Any]:
        """切换到已有会话并恢复它最近的普通 history。

        这一步只读该 session 目录下的 history journal/state；不会把当前会话内容
        保存到目标会话，也不会生成额外 history 事件。会话隔离边界完全由
        LocalSessionStore.switch_session 的目录校验保证。
        """
        if self.session_store is None:
            raise RuntimeError("local session store is not enabled")
        target_dir = self.session_store.resolve_session_dir(session_id)
        # 先用独立 journal 对目标目录完成校验和必要的崩溃恢复。只有恢复成功后才
        # 提交 active session 指针，避免目标损坏时把旧内存 history 留在新目录下。
        target_journal = HistoryJournal(lambda: target_dir)
        recovery = target_journal.recover(
            legacy_loader=lambda: load_legacy_history(target_dir),
        )
        summary = self.session_store.switch_session(session_id)
        self.history = recovery.history
        if self._history_journal is not None:
            self._history_journal.last_event_seq = recovery.last_event_seq
        if recovery.warnings:
            logger.warning("canonical history 恢复警告: %s", recovery.warnings)
        self._world_state_baseline = _world_state_from_history(self.history)
        # MemoryLoader 自身仍缓存文件解析结果，切换会话后需要显式失效。
        if self.memory_loader is not None:
            try:
                self.memory_loader.reset_cache(reason="switch_session")
            except Exception:
                logger.exception("MemoryLoader 缓存清理失败")
        return {
            "session": summary,
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
            "usage": self._session_usage_payload(),
            "plan_state": self.plan_state(),
            "subagent_tasks": self._subagent_tasks_payload(),
        }

    def compact_context(
        self,
        *,
        reason: str = "user_compact",
        active_turn_id: str = "",
        target_model: Optional[str] = None,
        target_context_limits: Optional[Dict[str, int]] = None,
        request_system_message: Any = _REQUEST_SNAPSHOT_UNSET,
        request_tools_schema: Any = _REQUEST_SNAPSHOT_UNSET,
        request_enabled_tools: Optional[frozenset[str]] = None,
    ) -> Dict[str, Any]:
        """摘要将被淘汰的旧前缀，并事务安装 canonical replacement。

        普通 compact 保留最近完整旧回合；mid-turn 还会把当前活动回合原样保留。
        摘要请求、journal replacement 和内存 generation 任一步失败时，原 history
        都保持不变。
        """

        compact_source = list(self.history.snapshot())
        before_messages = len(compact_source)
        if before_messages == 0:
            return {
                "session": self.current_session_payload().get("session"),
                "history": self.export_history(),
                "context_window": self.context_window_usage(),
                "plan_state": self.plan_state(),
                "summary": "",
                "before_messages": 0,
                "after_messages": 0,
                "persisted": False,
                "no_op": True,
            }

        summary_limits = self._context_limits()
        # Gateway 已经按唯一 ModelChoice.key 解析目标窗口时，必须直接使用该快照。
        # 仅兼容旧调用方时才按 model_id 回退，避免同名模型或自定义 provider 串配置。
        install_limits = (
            dict(target_context_limits)
            if isinstance(target_context_limits, dict) and target_context_limits
            else ConstantLLM.context_limits(target_model)
            if target_model else summary_limits
        )
        retained_target = dynamic_retained_token_target(install_limits["soft_limit_tokens"])
        source_tokens = estimate_message_tokens(compact_source)
        if (
            reason in {"manual", "user_compact"}
            and source_tokens <= retained_target
        ):
            return {
                "session": self.current_session_payload().get("session"),
                "history": self.export_history(),
                "context_window": self.context_window_usage(),
                "plan_state": self.plan_state(),
                "summary": "",
                "before_messages": before_messages,
                "after_messages": before_messages,
                "retained_tokens": source_tokens,
                "persisted": False,
                "no_op": True,
            }

        # mid-turn 必须沿用本用户回合首次请求的 system 快照。后台 MCP、权限或
        # Skill 索引即使中途变化，也只能在下一用户回合形成明确缓存边界。
        system_message = (
            self._static_system_message()
            if request_system_message is _REQUEST_SNAPSHOT_UNSET
            else copy.deepcopy(request_system_message)
        )
        installed_world_state = (
            self._current_world_state_snapshot(enabled_tools=request_enabled_tools)
            if reason == "mid_turn"
            else EMPTY_WORLD_STATE
        )

        world_state_message = (
            _make_context_update_message(
                _format_context_sections(list(installed_world_state.sections.items()), []),
                installed_world_state,
            )
            if installed_world_state.sections else None
        )
        tools_schema = (
            self._stable_tools_schema(
                self._filter_tools_schema_for_plan_mode(
                    self.registry.get_tools_description_openai_schema()
                    if self.llm.is_Function_Calling else None
                )
            )
            if request_tools_schema is _REQUEST_SNAPSHOT_UNSET
            else copy.deepcopy(request_tools_schema)
        )
        fixed_messages: List[Dict[str, Any]] = []
        if system_message:
            fixed_messages.append(system_message)
        if world_state_message is not None:
            fixed_messages.append(world_state_message.to_dict())
        if active_turn_id:
            fixed_messages.extend(
                message.to_dict()
                for message in compact_source
                if ConversationHistory.turn_id(message) == active_turn_id
            )
        fixed_tokens = self._estimate_request_tokens(fixed_messages, tools_schema)
        # 为交接摘要预留一块有界空间。实际摘要若更长，下面会扩大摘要前缀并重试
        # 分区；绝不通过静默删除 retained_tail 来强行满足窗口。
        summary_reserve = min(
            16 * 1024,
            max(2 * 1024, int(install_limits.get("max_output_tokens") or 0)),
        )
        retained_budget = min(
            retained_target,
            max(
                0,
                install_limits["soft_limit_tokens"] - fixed_tokens - summary_reserve,
            ),
        )

        model_result = None
        partition = None
        summary_message = None
        replacement_history: List[Message] = []
        post_tokens = 0
        # 实际摘要长度不可预知。最多重新分区三次，每次新增的淘汰消息都会进入
        # 新摘要请求；命中上限则显式失败，原 history 不会被替换。
        for _attempt in range(3):
            partition = partition_history_for_compaction(
                compact_source,
                retained_token_budget=retained_budget,
                active_turn_id=active_turn_id if reason == "mid_turn" else "",
            )
            if not partition.summarized_prefix:
                return {
                    "session": self.current_session_payload().get("session"),
                    "history": self.export_history(),
                    "context_window": self.context_window_usage(),
                    "plan_state": self.plan_state(),
                    "summary": "",
                    "before_messages": before_messages,
                    "after_messages": before_messages,
                    "retained_tokens": partition.retained_tokens,
                    "active_turn_tokens": partition.active_tokens,
                    "oversized_latest_turn": partition.oversized_latest_turn,
                    "persisted": False,
                    "no_op": True,
                }

            model_result = run_local_compaction(
                llm=self.llm,
                system_message=system_message,
                history=partition.summarized_prefix,
                hard_limit_tokens=summary_limits["hard_limit_tokens"],
                estimate_request_tokens=lambda request: self._estimate_request_tokens(
                    request,
                    None,
                ),
            )
            summary_message = make_summary_message(model_result.summary, reason=reason)
            replacement_history = [*partition.retained_tail]
            if world_state_message is not None:
                replacement_history.append(world_state_message)
            replacement_history.extend(partition.active_turn)
            replacement_history.append(summary_message)

            post_messages = ([system_message] if system_message else []) + [
                message.to_dict() for message in replacement_history
            ]
            post_tokens = self._estimate_request_tokens(post_messages, tools_schema)
            if post_tokens <= install_limits["soft_limit_tokens"]:
                break
            if not partition.retained_tail:
                raise CompactionError(
                    "当前活动回合与摘要已经超过目标模型 soft limit，"
                    "拒绝压缩当前工具现场"
                )
            overflow = post_tokens - install_limits["soft_limit_tokens"]
            retained_budget = max(0, retained_budget - max(1024, overflow * 2))
        else:
            raise CompactionError("compact 在三次完整重分区后仍无法满足目标窗口")

        assert partition is not None
        assert model_result is not None
        assert summary_message is not None
        after_messages = len(replacement_history)

        self._replace_history(
            replacement_history,
            reason=reason,
            metadata={
                "before_messages": before_messages,
                "after_messages": after_messages,
                "tokens_before": source_tokens,
                "tokens_after": estimate_message_tokens(replacement_history),
                "summarized_messages": len(partition.summarized_prefix),
                "retained_messages": len(partition.retained_tail),
                "active_turn_messages": len(partition.active_turn),
                "target_model": str(
                    target_model or getattr(self.llm, "model", "") or ""
                ),
            },
        )
        self._world_state_baseline = installed_world_state
        if self.session_store is not None:
            try:
                # state.json 只服务 UI。canonical journal 已提交后，状态预览失败
                # 不能把一次成功 replacement 对外伪装成失败。
                self.session_store.record_compaction_state(
                    summary=str(summary_message.content or ""),
                    reason=reason,
                )
            except Exception:
                logger.exception("compact UI 状态更新失败")
        if self.memory_loader is not None:
            try:
                self.memory_loader.reset_cache(reason=reason)
            except Exception:
                logger.exception("MemoryLoader 缓存清理失败")

        return {
            "session": self.current_session_payload().get("session"),
            "history": self.export_history(),
            "context_window": self.context_window_usage(),
            "plan_state": self.plan_state(),
            "summary": str(summary_message.content or ""),
            "before_messages": before_messages,
            "after_messages": after_messages,
            "retained_tokens": partition.retained_tokens,
            "retained_target_tokens": retained_target,
            "active_turn_tokens": partition.active_tokens,
            "oversized_latest_turn": partition.oversized_latest_turn,
            "world_state_sections": len(installed_world_state.sections),
            "attempts": model_result.attempts,
            "compact_strategy": model_result.strategy,
            "summary_requests": model_result.summary_requests,
            "summary_prompt_tokens": model_result.summary_prompt_tokens,
            "summary_output_tokens": model_result.summary_output_tokens,
            "source_message_count": model_result.source_message_count,
            "covered_message_count": model_result.covered_message_count,
            "model": str(getattr(self.llm, "model", "") or ""),
            "target_model": str(target_model or getattr(self.llm, "model", "") or ""),
            "post_request_tokens": post_tokens,
            "persisted": self._history_journal is not None,
            "no_op": False,
        }

    def chat(
        self,
        user_query: str,
        cancel_token: Optional[CancelToken] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,  # 可选附件列表
        persistent_user_text: Optional[str] = None, 
    ) -> str:
        """处理一次用户输入，返回最终答案字符串。

        全程经 self.event_bus 派发事件，本方法不直接输出任何字符。

        Args:
            cancel_token: 可选取消令牌。调 .cancel() 后：
              - LLM 流式：主动关闭连接，在统一落盘后 emit Cancelled
              - 工具循环：Bash/MCP 会收到运行时取消；普通进程内工具协作退出或等待返回
              - 所有工具终态和 transcript 提交后统一 emit Cancelled
              - 进入新一轮 think 之前会 abort 整个循环
            没传则新建一个空 token——chat 内部自己用，不会被外部触发。

        中断后 chat() 仍正常返回，Cancelled 事件会通过 event_bus 通知前端。
        """
        token = cancel_token if cancel_token is not None else CancelToken()
        self.current_cancel_token = token
        # 让工具内部 get_current_cancel_token() 拿到这个 token；
        # ToolExecutor 的并发分支会 copy_context 给 worker 用同一份 ContextVar
        ctx_token = set_current_cancel_token(token)
        parent_ctx_token = set_current_parent_session_id(self.current_runtime_session_id())
        image_ctx_token = set_pending_image_buffer()
        try:
            return self._chat_impl(
                user_query,
                token,
                attachments=attachments,  # 可选附件列表
                persistent_user_text=persistent_user_text,
            )
        finally:
            reset_pending_image_buffer(image_ctx_token)
            reset_current_parent_session_id(parent_ctx_token)
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

    def _static_system_message(
        self,
        *,
        enabled_tools: Optional[frozenset[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """构造稳定请求外壳中的 system message。"""

        stable_enabled_tools = (
            enabled_tools
            if enabled_tools is not None
            else frozenset(self._enabled_tools_for_prompt())
        )
        static_parts = get_static_system_prompt(enabled_tools=stable_enabled_tools)
        static_system = "\n\n".join(part.strip() for part in static_parts if part and part.strip())
        if self.system_prompt_addendum.strip():
            static_system = (
                f"{static_system}\n\n{self.system_prompt_addendum.strip()}"
                if static_system else self.system_prompt_addendum.strip()
            )
        return {"role": "system", "content": static_system} if static_system else None

    def _prepare_turn_input(
        self,
        *,
        user_content: Any,
        runtime_guidance: str,
        memory_query: str,
        hook_context: str = "",
        extra_evidence: Sequence[Message] = (),
        enabled_tools: Optional[frozenset[str]] = None,
    ) -> PreparedTurnInput:
        """生成即将原子追加到 history 的用户回合批次。

        ``world_state`` 只描述当前仍成立的环境；RAG、hook 等回合证据同样进入
        history，但不会污染下一次环境 diff 的基线。
        """

        stable_enabled_tools = (
            enabled_tools
            if enabled_tools is not None
            else frozenset(self._enabled_tools_for_prompt())
        )
        dynamic_builder_error: Optional[BaseException] = None
        try:
            dynamic_sections = self._run_context_coro(
                get_dynamic_context_sections(
                    enabled_tools=stable_enabled_tools,
                    model=getattr(self.llm, "model", "") or "",
                    cwd=Path.cwd(),
                    memory_loader=self.memory_loader if self.ctx_enabled else None,
                    mcp_clients=self.mcp_clients,
                    skill_commands=[],
                    language=self.language,
                    memory_query=memory_query,
                )
            )
        except Exception as error:
            logger.exception("dynamic context prompt build failed")
            dynamic_sections = []
            dynamic_builder_error = error

        context_sections: List[DynamicSectionResult] = list(dynamic_sections)
        if runtime_guidance.strip():
            context_sections.append(DynamicSectionResult.present(
                "runtime_guidance",
                runtime_guidance.strip(),
            ))
        plan_context = self._plan_context_text()
        context_sections.append(
            DynamicSectionResult.present("plan", plan_context.strip())
            if plan_context else DynamicSectionResult.absent("plan")
        )

        world_values = dict(self._world_state_baseline.sections)
        evidence: List[Message] = list(extra_evidence)
        if hook_context.strip():
            evidence.append(_make_context_evidence_message("hooks", hook_context))

        for section in context_sections:
            if not isinstance(section, DynamicSectionResult):
                raise TypeError("动态上下文必须返回 DynamicSectionResult")
            key = str(section.name or "").strip()
            if not key:
                continue
            if section.scope == "turn_evidence":
                if section.status == "present" and section.text.strip():
                    evidence.append(_make_context_evidence_message(key, section.text))
                elif section.status == "error":
                    logger.warning("回合证据读取失败: name=%s error=%s", key, section.error)
                continue
            if section.status == "present" and section.text.strip():
                world_values[key] = section.text.strip()
            elif section.status == "absent":
                world_values.pop(key, None)
            elif section.status == "error":
                logger.warning("环境 section 读取失败，沿用 baseline: name=%s error=%s", key, section.error)
                if key == "instructions" and key not in self._world_state_baseline.sections:
                    raise RuntimeError(
                        "关键 instructions 首次读取失败，已阻止本轮模型请求: "
                        f"{section.error or 'unknown error'}"
                    )

        if (
            dynamic_builder_error is not None
            and self.ctx_enabled
            and self.memory_loader is not None
            and "instructions" not in self._world_state_baseline.sections
        ):
            raise RuntimeError(
                "动态上下文整体构建失败，且没有可沿用的 instructions baseline，"
                "已阻止本轮模型请求"
            ) from dynamic_builder_error

        current_world_state = WorldStateSnapshot(sections=world_values)
        world_diff = current_world_state.diff(self._world_state_baseline)
        batch: List[Message] = []
        if world_diff.changed or world_diff.removed:
            batch.append(_make_context_update_message(
                _format_context_sections(world_diff.changed, world_diff.removed),
                current_world_state,
            ))
        batch.extend(evidence)
        batch.append(Message(role=MessageRole.USER, content=copy.deepcopy(user_content)))
        return PreparedTurnInput(messages=tuple(batch), world_state=current_world_state)

    def _provider_request_messages(
        self,
        extra_messages: Sequence[Message] = (),
        *,
        allow_pending_tool_tail: bool = False,
        system_message: Any = _REQUEST_SNAPSHOT_UNSET,
    ) -> List[Dict[str, Any]]:
        """从稳定外壳和唯一 history 生成一次性 provider 请求。"""

        messages: List[Dict[str, Any]] = []
        stable_system = (
            self._static_system_message()
            if system_message is _REQUEST_SNAPSHOT_UNSET
            else copy.deepcopy(system_message)
        )
        if stable_system is not None:
            messages.append(stable_system)
        messages.extend(self.history.provider_messages())
        messages.extend(copy.deepcopy(message.to_dict()) for message in extra_messages)
        validate_tool_protocol(
            messages,
            allow_pending_tail=allow_pending_tool_tail,
        )
        return messages

    @staticmethod
    def _run_context_coro(coro):
        """在同步 session 主链路中执行动态上下文的异步组装。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        # 当前线程已有运行中的 loop 时，不能再嵌套 run_until_complete。使用短线程
        # 执行独立 loop，既保持同步接口，也保证传入 coroutine 一定被等待或抛错。
        result: Dict[str, Any] = {}

        def _worker() -> None:
            try:
                result["value"] = asyncio.run(coro)
            except BaseException as exc:  # noqa: BLE001
                result["error"] = exc

        worker = threading.Thread(target=_worker, name="cbagent-context-builder", daemon=True)
        worker.start()
        worker.join()
        error = result.get("error")
        if error is not None:
            raise error
        return result.get("value")

    def _baseline_dynamic_sections(
        self,
        enabled_tools: frozenset[str],
    ) -> List[DynamicSectionResult]:
        """读取不依赖下一条用户输入的动态 sections，供空闲态 Context 估算。"""
        try:
            sections = self._run_context_coro(
                get_dynamic_context_sections(
                    enabled_tools=enabled_tools,
                    model=getattr(self.llm, "model", "") or "",
                    cwd=Path.cwd(),
                    memory_loader=self.memory_loader if self.ctx_enabled else None,
                    mcp_clients=self.mcp_clients,
                    skill_commands=[],
                    language=self.language,
                    memory_query="",
                )
            )
        except Exception:
            logger.exception("空闲态动态上下文构建失败")
            sections = []
        return list(sections)

    def _current_world_state_snapshot(
        self,
        *,
        enabled_tools: Optional[frozenset[str]] = None,
    ) -> WorldStateSnapshot:
        """读取 compact 当下仍成立的完整现场，读取失败时沿用已见基线。"""

        values = dict(self._world_state_baseline.sections)
        stable_enabled_tools = (
            enabled_tools
            if enabled_tools is not None
            else frozenset(self._enabled_tools_for_prompt())
        )
        for section in self._baseline_dynamic_sections(stable_enabled_tools):
            if not isinstance(section, DynamicSectionResult):
                logger.error("compact 动态上下文返回了非法类型: %s", type(section).__name__)
                continue
            if section.scope != "world_state":
                continue
            key = str(section.name or "").strip()
            if not key:
                continue
            if section.status == "present" and section.text.strip():
                values[key] = section.text.strip()
            elif section.status == "absent":
                values.pop(key, None)
            elif section.status == "error":
                logger.warning(
                    "compact 现场读取失败，沿用 baseline: name=%s error=%s",
                    key,
                    section.error,
                )
        plan_context = self._plan_context_text().strip()
        if plan_context:
            values["plan"] = plan_context
        else:
            values.pop("plan", None)
        return WorldStateSnapshot(sections=values)

    def _enabled_tools_for_prompt(self) -> List[str]:
        """返回当前模式下应暴露给 LLM 的工具名列表。

        execute 模式：返回全部注册工具。
        plan 模式：只返回 PLAN_READ_TOOLS + PLAN_READ_ACTIONS 的键 + bash。
        这决定 system prompt 中 "Available tools:" 段的内容。
        注意：工具 schema 过滤在 _filter_tools_schema_for_plan_mode 中独立完成，
        这里只控制名称列表。
        """
        tools = self.registry.list_tools()
        if self.collaboration_mode() != "plan":
            return tools
        allowed = set(PLAN_READ_TOOLS) | set(PLAN_READ_ACTIONS.keys()) | {"bash"}
        return [name for name in tools if name in allowed]

    def _filter_tools_schema_for_plan_mode(
        self,
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """在 Plan Mode 下过滤 OpenAI tools schema，仅保留只读工具。

        plan 模式：
        - 移除所有不在白名单中的工具（模型根本看不到它们）
        - 对保留的工具，在 description 末尾追加 Plan Mode 使用限制提示
          （如 bash → "only non-mutating exploration commands"）
        - 深拷贝 schema entry 后再修改，不污染原始 ToolRegistry 缓存

        execute 模式或 tools_schema 为 None → 原样返回。
        """
        if tools_schema is None or self.collaboration_mode() != "plan":
            return tools_schema
        allowed = set(PLAN_READ_TOOLS) | set(PLAN_READ_ACTIONS.keys()) | {"bash"}
        filtered: List[Dict[str, Any]] = []
        for entry in tools_schema:
            fn = entry.get("function") if isinstance(entry, dict) else None
            name = str((fn or {}).get("name") or "")
            if name not in allowed:
                continue
            # 深拷贝后追加 Plan Mode 注释，不污染 registry 原始 schema
            cloned = copy.deepcopy(entry)
            cloned_fn = cloned.get("function") if isinstance(cloned, dict) else None
            if isinstance(cloned_fn, dict):
                note = self._plan_tool_note(name)
                if note:
                    desc = str(cloned_fn.get("description") or "")
                    cloned_fn["description"] = (desc + "\n\n" + note).strip()
            filtered.append(cloned)
        return filtered

    @staticmethod
    def _stable_tools_schema(
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> Optional[List[Dict[str, Any]]]:
        """按 function.name 稳定排序工具 schema，保证完整请求前缀可复现。"""
        if tools_schema is None:
            return None

        def _sort_key(entry: Dict[str, Any]) -> tuple[str, str]:
            function = entry.get("function") if isinstance(entry, dict) else None
            name = str((function or {}).get("name") or "")
            # 名称异常或重复时，用稳定 JSON 作为次级键，避免注册顺序泄漏进请求。
            serialized = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
            return name, serialized

        return [
            copy.deepcopy(entry)
            for entry in sorted(tools_schema, key=_sort_key)
        ]

    def _plan_tool_note(self, tool_name: str) -> str:
        """为 Plan Mode 下的工具生成 description 补充说明。

        - bash: 详细说明只允许非修改性探索命令
        - PLAN_READ_ACTIONS 中的工具: 列出允许的具体 action
        - 其他白名单工具: 通用"只读/探索"提示
        """
        if tool_name == "bash":
            return (
                "Plan Mode: only non-mutating exploration commands are allowed. "
                "Do not use redirection, background execution, installs, git checkout/reset/pull/push, "
                "or filesystem mutation commands."
            )
        if tool_name in PLAN_READ_ACTIONS:
            actions = ", ".join(sorted(PLAN_READ_ACTIONS[tool_name]))
            return f"Plan Mode: only read-only actions are allowed: {actions}."
        return "Plan Mode: use this only for reading or exploration, not for modification."

    def _plan_context_text(self) -> str:
        """生成 Plan Mode 上下文，注入到每轮 chat 的 context_update 消息中。

        两段内容:
        1. Plan Mode 行为指令（仅在 plan 模式下注入）
           - 告知 LLM 当前处于计划模式
           - 明确允许和禁止的操作
           - 指示使用 <proposed_plan> 块提交计划
           - 提示服务端会拒绝写入类工具调用
        2. PlanStateStore.context_text()（状态摘要）
           - pending/rejected/approved 状态的计划内容摘要
           - 拒绝反馈（如有）
        """
        if self.is_subagent:
            # 子代理没有独立的持久化会话，PlanStateStore 会回退到项目级目录。
            # 这里必须在读取前退出，避免把父会话计划注入子代理上下文。
            return ""
        state = self.plan_store.load(include_content=True)
        mode = str(state.get("mode") or "execute")
        state_text = self.plan_store.context_text()
        parts: List[str] = []
        if mode == "plan":
            parts.append(
                "[Plan Mode Instructions]\n"
                "You are in Plan Mode. Your job is to investigate, ask clarifying questions when needed, "
                "and produce an implementation plan for the user to approve.\n"
                "- You may read files, search, inspect state, ask the user questions, and run read-only bash.\n"
                "- You must not modify files, write code, update todo/memory/knowledge, start background tasks, "
                "delegate to execution agents, or call mutating MCP/tools.\n"
                "- Server-side policy will reject mutating tool calls; treat rejections as a signal to continue "
                "with read-only exploration.\n"
                "- When ready, put the complete Markdown plan inside exactly one "
                "<proposed_plan>...</proposed_plan> block. The plan should be self-contained, actionable, "
                "and include verification steps.\n"
                "- If the user rejected a previous plan, address the feedback and submit a complete replacement plan."
            )
        if state_text:
            parts.append(state_text)
        return "\n\n".join(parts)

    def _save_pending_plan(self, plan: str, *, round_idx: int = 0) -> Dict[str, Any]:
        """持久化 pending plan 并 emit PlanReady 事件。

        在两种路径中被调用：
        1. 流式路径：_PlanParsingEventBus.finish() 检测到 </proposed_plan>
        2. 非流式路径：_handle_plan_blocks_in_answer() 在完整回答中提取到计划块
        """
        state = self.plan_store.save_pending_plan(plan)
        self.event_bus.emit(PlanReady(plan=plan, plan_state=state, round_idx=round_idx))
        return state

    def _plan_execution_policy(self) -> Optional[PlanExecutionPolicy]:
        """Plan Mode 下返回 PlanExecutionPolicy，execute 模式返回 None。

        None 意味着 ToolExecutor 不启用策略检查，所有工具正常执行。
        PlanExecutionPolicy 实例在每次 _tool_loop 的 executor.execute() 调用时
        传入，在 _run_one 中对每个工具做 check() 硬拦截。
        """
        if self.collaboration_mode() == "plan":
            return PlanExecutionPolicy()
        return None

    def _handle_plan_blocks_in_answer(
        self,
        answer: str,
        *,
        round_idx: int,
        plan_bus: Optional[_PlanParsingEventBus] = None,
    ) -> str:
        """处理非流式 LLM 回答中可能包含的 <proposed_plan> 块。

        用于两种场景：
        1. 不支持 FC 的模型（result 是 list），回答一次性返回
        2. FC 模型的最终 answer 字段

        从 answer 中分离：
        - visible_text: 计划块外的普通回答 → 返回给用户
        - plan: 最后一个 <proposed_plan> 块的内容 → 持久化并 emit PlanReady

        如果流式解析（plan_bus）已经保存过同一份计划，跳过重复保存。
        """
        if self.collaboration_mode() != "plan" or not answer:
            return answer
        visible, plan = split_proposed_plan_text(answer)
        if plan is not None and plan.strip():
            proposed = plan.strip()
            # 去重：流式路径可能已经通过 plan_bus.finish() 保存过
            saved = (plan_bus.saved_plan_text or "").strip() if plan_bus is not None else ""
            if proposed != saved:
                self._save_pending_plan(proposed, round_idx=round_idx)
        return visible

    def _chat_impl(
        self,
        user_query: str,
        token: CancelToken,
        attachments: Optional[List[Dict[str, Any]]] = None,
        persistent_user_text: Optional[str] = None,
    ) -> str:
        del persistent_user_text  # 模型可见 user 与持久 history 不再使用两套文本。
        chat_started = time.perf_counter()
        explicit_skill_query = str(user_query or "")
        user_query = self._prepend_runtime_notifications(explicit_skill_query)
        turn_id = uuid.uuid4().hex

        hook_extra_context = ""
        if self.hook_manager is not None:
            if not self._session_start_fired:
                self._session_start_fired = True
                if self.hook_manager.has_event("SessionStart"):
                    outcome = self.hook_manager.fire(
                        "SessionStart",
                        {"source": "startup"},
                        matcher_value="startup",
                    )
                    hook_extra_context = str(outcome.additional_context or "")
            if self.hook_manager.has_event("UserPromptSubmit"):
                outcome = self.hook_manager.fire("UserPromptSubmit", {"prompt": user_query})
                if outcome.blocked or outcome.stop:
                    reason = outcome.block_reason or "本次输入被 hooks 配置拦截。"
                    self.event_bus.emit(Done(
                        final_answer=reason,
                        rounds_used=0,
                        cancelled=False,
                    ))
                    return reason
                if outcome.additional_context:
                    hook_extra_context += (
                        ("\n" if hook_extra_context else "")
                        + str(outcome.additional_context)
                    )

        active_model = getattr(self.llm, "active_model_config", None)
        multimodal_prompt = process_multimodal_prompt(
            text=user_query,
            attachments=attachments,
            model=getattr(self.llm, "model", None),
            soft_limit_tokens=self._context_limits()["soft_limit_tokens"],
            image_ability=(
                bool(active_model.image_ability)
                if active_model is not None and hasattr(active_model, "image_ability")
                else None
            ),
        )
        request_content = multimodal_prompt.request_content
        explicit_skill_evidence = self._explicit_skill_evidence(explicit_skill_query)
        history_user_text = explicit_skill_query
        enabled_tools_snapshot = frozenset(self._enabled_tools_for_prompt())
        runtime_guidance = self._build_system_instructions()
        prepared = self._prepare_turn_input(
            user_content=request_content,
            runtime_guidance=runtime_guidance,
            memory_query=history_user_text,
            hook_context=hook_extra_context,
            extra_evidence=explicit_skill_evidence,
            enabled_tools=enabled_tools_snapshot,
        )

        # system 与 tools schema 在本用户回合开始时冻结。普通工具循环只追加 history，
        # 不允许后台注册表或运行态变化在中途改写请求外壳。
        request_system_message = self._static_system_message(
            enabled_tools=enabled_tools_snapshot,
        )
        tools_schema = self._stable_tools_schema(
            self._filter_tools_schema_for_plan_mode(
                self.registry.get_tools_description_openai_schema()
                if self.llm.is_Function_Calling else None
            )
        )
        candidate_messages = self._provider_request_messages(
            prepared.messages,
            system_message=request_system_message,
        )
        auto_compactions: List[Dict[str, Any]] = []
        preflight = self._maybe_auto_compact_preflight(
            user_query=user_query,
            system_instructions=runtime_guidance,
            messages=candidate_messages,
            tools_schema=tools_schema,
        )
        if preflight is not None:
            auto_compactions.append(preflight)
            if preflight.get("blocked"):
                blocked_message = (
                    "[上下文窗口已满] 自动 compact 已无法为本轮输入释放足够空间。"
                )
                self.event_bus.emit(Done(
                    final_answer=blocked_message,
                    rounds_used=0,
                    cancelled=False,
                    context_window=self.context_window_usage(),
                    auto_compact={"compacted": True, "events": auto_compactions},
                ))
                return blocked_message
            # pre-turn compact 会重置 world-state baseline，必须重新生成完整现场批次。
            prepared = self._prepare_turn_input(
                user_content=request_content,
                runtime_guidance=runtime_guidance,
                memory_query=history_user_text,
                hook_context=hook_extra_context,
                extra_evidence=explicit_skill_evidence,
                enabled_tools=enabled_tools_snapshot,
            )

        self._append_history(
            prepared.messages,
            turn_id=turn_id,
            event_kind="turn_input",
        )
        self._world_state_baseline = prepared.world_state
        request_messages = self._provider_request_messages(
            system_message=request_system_message,
        )
        if self.message_logger is not None:
            try:
                self.message_logger.log(
                    request_messages,
                    tools=tools_schema,
                    label=f"会话开始 | query=\"{history_user_text[:100]}\"",
                )
            except Exception:
                logger.exception("message_logger 写入失败")

        logger.info(
            "chat start: query_chars=%s attachments=%s history=%s tools=%s generation=%s",
            len(user_query),
            len(multimodal_prompt.attachments),
            len(self.history),
            len(tools_schema or []),
            self.history.generation,
        )
        try:
            rounds_used, final_answer, trace_collector, loop_compactions = self._tool_loop(
                tools_schema,
                token,
                turn_id=turn_id,
                request_system_message=request_system_message,
                request_enabled_tools=enabled_tools_snapshot,
            )
        except LLMRequestError as exc:
            error_text = self._format_llm_request_error(exc)
            partial = str(getattr(exc, "partial_answer", "") or "")
            failure_content = (
                "<turn_failed>\n"
                f"{error_text}\n"
                + (f"已收到但未完成的模型文本：\n{partial}\n" if partial else "")
                + "本轮没有得到完整模型响应；不要假设未记录的工具已经执行。\n"
                "</turn_failed>"
            )
            self._append_history([
                Message(
                    role=MessageRole.USER,
                    content=failure_content,
                    metadata={"kind": "turn_failed", "reason": type(exc).__name__},
                )
            ], turn_id=turn_id, event_kind="turn_failed")
            self.event_bus.emit(Error(
                where="llm",
                message=error_text,
                round_idx=int(getattr(exc, "round_idx", 0) or 0),
            ))
            self.event_bus.emit(Done(
                final_answer=error_text,
                rounds_used=int(getattr(exc, "round_idx", 0) or 0),
                cancelled=False,
                context_window=self.context_window_usage(),
                auto_compact={
                    "compacted": bool(auto_compactions),
                    "events": auto_compactions,
                } if auto_compactions else None,
            ))
            return error_text

        auto_compactions.extend(loop_compactions)

        if token.is_cancelled():
            # 中断标记必须晚于已经完成的 tool results，并早于 Cancelled/Done 事件。
            # 下一轮模型据此知道上一条 user 已明确终止，不能把它当作待继续任务；
            # export_history 会过滤该内部控制消息，不会在前端伪装成用户输入。
            cancel_reason = (
                token.reason.value
                if getattr(token, "reason", None) is not None
                else "user_cancelled"
            )
            self._append_history(
                [Message(
                    role=MessageRole.USER,
                    content=(
                        f'<turn_aborted reason="{cancel_reason}">\n'
                        "本轮已中止；已记录的工具结果可能已经产生副作用，"
                        "不得自动重放。\n"
                        "</turn_aborted>"
                    ),
                    metadata={
                        "kind": "turn_aborted",
                        "reason": cancel_reason,
                        "interrupted": True,
                    },
                )],
                turn_id=turn_id,
                event_kind="turn_aborted",
            )

        work_record = self._make_work_record(
            user_query=history_user_text,
            final_answer=final_answer,
            trace_collector=trace_collector,
        )
        if self.session_store is not None:
            self.session_store.commit_turn_state(
                user_query=history_user_text,
                work_record=work_record,
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

        # 本轮结束后再看一次 state/history。工具轨迹索引和现场变化可能让下一轮
        # 请求达到安全窗口；此时执行正式 replacement，旧 generation 仍留在
        # history.jsonl 供审计，下一轮从新 generation 继续追加。
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

        # 中断事件必须晚于 canonical history 提交，前端收到后即可安全释放 busy。
        if token.is_cancelled():
            self.event_bus.emit(Cancelled(
                where="session", round_idx=rounds_used,
            ))

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
        # 同时清空运行时 section 指纹与 MemoryLoader memoize，让下一轮完整重注入
        # 动态上下文，并重新读取 CLAUDE.md 等记忆文件。
        owner_session_id = self.current_runtime_session_id()
        if self.subagent_task_registry is not None:
            try:
                self.subagent_task_registry.cancel_owner(owner_session_id)
            except Exception:
                logger.exception("清理会话时取消子代理任务失败")
        if self.session_store is not None:
            # 删除 active session 是清理事务的磁盘提交点。失败必须向上抛出，且在
            # 此之前不能清空内存 history，否则后续请求会在旧 journal 上追加空基线。
            self.session_store.clear_active_session()
        self.history.clear_memory()
        self._world_state_baseline = EMPTY_WORLD_STATE
        if self._history_journal is not None:
            self._history_journal.last_event_seq = 0
        # 持久化会话已整体删除，PlanStateStore 此时读取到自然空状态；无 session
        # store 的嵌入入口仍显式清理 fallback plan 目录。
        state = (
            self.plan_store.load(include_content=True)
            if self.session_store is not None
            else self.plan_store.clear()
        )
        self.event_bus.emit(PlanModeChanged(
            mode=state.get("mode", "execute"),
            plan_state=state,
        ))
        if self.memory_loader is not None:
            try:
                self.memory_loader.reset_cache(reason="clear_history")
            except Exception:
                logger.exception("MemoryLoader 缓存清理失败")

    def _model_max_tokens(self) -> int:
        """返回当前模型声明的完整上下文窗口。

        优先读 llm.active_model_config（按 ModelChoice.key 绑定）；无 active
        config 时再回退 ConstantLLM 的 model_id/env 路径。
        """
        active = getattr(self.llm, "active_model_config", None)
        if active is not None and getattr(active, "max_context_tokens", None):
            return max(1, int(active.max_context_tokens))
        limits = getattr(self.llm, "active_context_limits", None)
        if callable(limits):
            try:
                return max(1, int(limits()["full_window_tokens"]))
            except Exception:
                pass
        return ConstantLLM.model_max_tokens(getattr(self.llm, "model", None))

    def _context_budget_tokens(self) -> int:
        """返回动态 soft limit，兼容旧内部方法名。"""
        return self._context_limits()["soft_limit_tokens"]

    def _context_limits(self) -> Dict[str, int]:
        """返回当前模型统一的完整窗口与 soft/hard 边界。

        优先使用 llm.active_context_limits()，保证 Footer、compact 预算和真实
        provider 请求读取同一份 ModelChoice 运行时配置。
        """
        limits_fn = getattr(self.llm, "active_context_limits", None)
        if callable(limits_fn):
            try:
                candidate = dict(limits_fn())
                required = {
                    "full_window_tokens",
                    "max_output_tokens",
                    "estimation_margin_tokens",
                    "soft_limit_tokens",
                    "hard_limit_tokens",
                }
                if required.issubset(candidate) and all(
                    int(candidate[key]) > 0 for key in required - {"estimation_margin_tokens"}
                ):
                    return candidate
                logger.warning(
                    "active_context_limits 返回字段不完整，回退 ConstantLLM: keys=%s",
                    sorted(candidate),
                )
            except Exception:
                logger.exception("读取 active_context_limits 失败，回退 ConstantLLM")
        return ConstantLLM.context_limits(getattr(self.llm, "model", None))

    def _baseline_request_parts(self) -> tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
        """构造空闲态下一次请求的无副作用基线，不虚构用户输入。"""
        enabled_tools = frozenset(self._enabled_tools_for_prompt())
        # 工具执行期间 UI 仍可读取 Context；此时末尾 assistant.tool_calls 已经持久化，
        # 对应 tool 结果尚未产生，因此诊断快照允许唯一的 pending tail。
        messages = self._provider_request_messages(allow_pending_tool_tail=True)

        # 空闲态使用同一份 world state 规则计算下一请求会新增的现场内容。
        persistent_values = dict(self._world_state_baseline.sections)
        for section in self._baseline_dynamic_sections(enabled_tools):
            if not isinstance(section, DynamicSectionResult):
                logger.error("空闲态动态上下文返回了非法类型: %s", type(section).__name__)
                continue
            if section.scope != "world_state":
                continue
            key = str(section.name or "").strip()
            if not key:
                continue
            if section.status == "present" and section.text.strip():
                persistent_values[key] = section.text.strip()
            elif section.status == "absent":
                persistent_values.pop(key, None)
            # error 明确保留旧值；空闲态只做估算，不在这里阻断 UI payload。
        plan_context = self._plan_context_text()
        if plan_context:
            persistent_values["plan"] = plan_context.strip()
        else:
            persistent_values.pop("plan", None)
        # 空闲态估算同样不注入 session_state，与正式请求保持一致。
        current_world_state = WorldStateSnapshot(sections=persistent_values)
        world_diff = current_world_state.diff(self._world_state_baseline)
        if world_diff.changed or world_diff.removed:
            messages.append({
                "role": "user",
                "content": _format_context_update_text(
                    _format_context_sections(world_diff.changed, world_diff.removed)
                ),
            })

        tools_schema = self._stable_tools_schema(
            self._filter_tools_schema_for_plan_mode(
                self.registry.get_tools_description_openai_schema()
                if self.llm.is_Function_Calling else None
            )
        )
        return messages, tools_schema

    def _dynamic_context_text(self) -> str:
        """兼容调试调用，返回与空闲态完整请求基线一致的序列化文本。"""
        messages, tools_schema = self._baseline_request_parts()
        return json.dumps(
            {"messages": messages, "tools": tools_schema or []},
            ensure_ascii=False,
            default=str,
        )

    def context_window_usage(self) -> Dict[str, Any]:
        """估算空闲态下一次完整请求的基线占用。"""
        messages, tools_schema = self._baseline_request_parts()
        raw = self._estimate_request_tokens(messages, tools_schema)
        return self._context_window_payload(
            used_tokens=self._calibrated_request_tokens(raw),
            raw_estimated_tokens=raw,
            source="estimate",
            scope="next_request_baseline",
        )

    def _context_window_payload(
        self,
        *,
        used_tokens: int,
        raw_estimated_tokens: int,
        source: str,
        scope: str,
    ) -> Dict[str, Any]:
        """统一生成前后端 ContextWindow 结构。"""
        limits = self._context_limits()
        full_window = limits["full_window_tokens"]
        used = max(0, int(used_tokens))
        return {
            "used_tokens": used,
            "max_tokens": full_window,
            "full_window_tokens": full_window,
            "remaining_tokens": max(0, full_window - used),
            "percent": round(min(100.0, used / full_window * 100.0), 1),
            "source": source,
            "scope": scope,
            "model_max_tokens": full_window,
            "raw_estimated_tokens": max(0, int(raw_estimated_tokens)),
            "calibration_ratio": round(self._calibration_ratio(), 4),
            **limits,
            "auto_compact_trigger_tokens": limits["soft_limit_tokens"],
            "auto_compact_trigger_percent": round(
                limits["soft_limit_tokens"] / full_window * 100.0,
                1,
            ),
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

    def _request_context_window_usage(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        *,
        used_tokens: Optional[int] = None,
    ) -> Dict[str, Any]:
        """估算当前工具循环请求体占用，用于 UI 实时刷新 Context 指标。"""
        raw = (
            used_tokens
            if used_tokens is not None
            else self._estimate_request_tokens(messages, tools_schema)
        )
        return self._context_window_payload(
            used_tokens=self._calibrated_request_tokens(raw),
            raw_estimated_tokens=raw,
            source="estimate",
            scope="current_request",
        )

    def _emit_context_window_update(
        self,
        *,
        reason: str,
        round_idx: int,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools_schema: Optional[List[Dict[str, Any]]] = None,
        used_tokens: Optional[int] = None,
    ) -> None:
        """广播 Context 指标刷新；失败只记日志，不能打断工具循环。"""
        try:
            context_window = (
                self._request_context_window_usage(
                    messages,
                    tools_schema,
                    used_tokens=used_tokens,
                )
                if messages is not None
                else self.context_window_usage()
            )
            self.event_bus.emit(ContextWindowUpdated(
                context_window=context_window,
                reason=reason,
                round_idx=round_idx,
            ))
        except Exception:
            logger.exception("context_window_updated emit failed")

    def _explicit_skill_evidence(self, user_text: str) -> List[Message]:
        """读取用户显式点名的 Skill 正文，并返回正式回合证据消息。"""

        if self.skill_manager is None or not isinstance(user_text, str):
            return []
        try:
            skills = self.skill_manager.collect_explicit_mentions(user_text)
        except Exception:
            logger.exception("explicit skill mention collection failed")
            return []
        if not skills:
            return []

        evidence: List[Message] = []
        for skill in skills:
            try:
                content = self.skill_manager.load_skill_content(skill.name)
            except Exception:
                logger.exception("explicit skill load failed: %s", skill.name)
                continue
            if str(content or "").strip():
                evidence.append(_make_context_evidence_message(
                    f"skill:{skill.name}",
                    str(content),
                ))
        return evidence

    # ---------- 动态上下文阈值 ----------

    def _auto_compact_trigger_tokens(self) -> int:
        """返回自动 compact 的触发阈值（token 数）。"""
        return max(1, self._context_budget_tokens())

    def _full_window_blocking_threshold(self) -> int:
        """返回硬阻断阈值，即完整窗口扣除真实输出预留。"""
        return self._context_limits()["hard_limit_tokens"]

    def _auto_compact_history(
        self,
        *,
        reason: str,
        round_idx: int = 0,
        force: bool = False,
        request_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """执行一次自动跨轮 compact，并替换 active history。

        与手动 /compact 走同一条路径(compact_context),只是返回一份审计用的
        轻量事件给 Done.auto_compact 渲染。``force=True`` 用于 preflight:即便
        state/history 单独看没达到 budget,也强制触发。

        compact 前原始消息仍保留在 history.jsonl 的旧 generation 事件中；成功后
        内存 history 安装“最近完整回合 + 现场快照 + 活动回合 + handoff summary”。
        """
        before_usage = self.context_window_usage()
        budget = int(before_usage["max_tokens"])
        # 使用与 preflight 相同的触发阈值（auto_compact_trigger_tokens），
        # 而非旧的固定 buffer。这样 post_turn / 手动 / preflight 三条路径
        # 共享同一个触发标准，行为一致可预测。
        trigger_tokens = self._auto_compact_trigger_tokens()
        if not force and int(before_usage["used_tokens"]) < trigger_tokens:
            return None
        before_messages = len(self.history)
        if before_messages == 0:
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
            payload = self.compact_context(reason=reason)
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
            "trigger_tokens": trigger_tokens,
            "request_tokens": request_tokens,
            "persisted": bool(payload.get("persisted")),
        }

    def _maybe_auto_compact_history(
        self,
        *,
        reason: str,
        round_idx: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """当跨轮完整请求基线达到动态 soft limit 时自动 compact。"""
        return self._auto_compact_history(reason=reason, round_idx=round_idx, force=False)

    def _maybe_auto_compact_preflight(
        self,
        *,
        user_query: str,
        system_instructions: str,
        messages: List[Dict[str, Any]],
        tools_schema: Optional[List[Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        """仅在完整 canonical history 请求超过预算时触发 preflight compact。"""
        del user_query, system_instructions  # 估算直接读 messages
        raw_request_tokens = self._estimate_request_tokens(messages, tools_schema)
        request_tokens = self._calibrated_request_tokens(raw_request_tokens)
        full_window = self._model_max_tokens()
        budget_tokens = self._context_budget_tokens()

        if request_tokens <= budget_tokens:
            return None

        event = self._auto_compact_history(
            reason="preflight_context_overflow",
            round_idx=0,
            force=True,
                request_tokens=request_tokens,
        )
        if event is not None:
            event["full_window"] = full_window
            event["budget_tokens"] = budget_tokens
            return event

        blocking_threshold = self._full_window_blocking_threshold()
        if request_tokens >= blocking_threshold:
            logger.error(
                "blocking limit reached: request=%s window=%s hard_limit=%s",
                request_tokens, full_window, blocking_threshold,
            )
            self.event_bus.emit(Error(
                where="session",
                message=(
                    f"上下文窗口即将耗尽 (请求 {request_tokens} tokens >= "
                    f"{blocking_threshold}),"
                    "且自动 compact 无法继续释放空间。请手动 /clear 或 /compact 后重试。"
                ),
                round_idx=0,
            ))
            return {
                "reason": "blocking_limit",
                "round_idx": 0,
                "request_tokens": request_tokens,
                "full_window": full_window,
                "blocking_threshold": blocking_threshold,
                "blocked": True,
            }

        return None

    def _mid_turn_compact(
        self,
        *,
        round_idx: int,
        request_tokens: int,
        active_turn_id: str,
        tools_schema: Optional[List[Dict[str, Any]]],
        request_system_message: Optional[Dict[str, Any]],
        request_enabled_tools: frozenset[str],
    ) -> Optional[Dict[str, Any]]:
        """在完整工具批次边界压缩旧回合，当前活动回合始终原样保留。"""

        before_messages = len(self.history)
        try:
            payload = self.compact_context(
                reason="mid_turn",
                active_turn_id=active_turn_id,
                request_system_message=request_system_message,
                request_tools_schema=tools_schema,
                request_enabled_tools=request_enabled_tools,
            )
        except Exception:
            logger.exception("工具循环 mid-turn compact 失败")
            return None
        if payload.get("no_op"):
            return None

        # replacement 安装完成后，从唯一 history 重新派生 provider 请求。
        # 这是本轮唯一允许发生的前缀重建边界。
        after_messages = self._provider_request_messages(
            system_message=request_system_message,
        )
        after_raw = self._estimate_request_tokens(after_messages, tools_schema)
        return {
            "reason": "mid_turn",
            "round_idx": round_idx,
            "before_messages": before_messages,
            "after_messages": len(self.history),
            "before_tokens": request_tokens,
            "after_tokens": self._calibrated_request_tokens(after_raw),
            "raw_after_tokens": after_raw,
            "soft_limit_tokens": self._context_limits()["soft_limit_tokens"],
            "hard_limit_tokens": self._context_limits()["hard_limit_tokens"],
            "persisted": bool(payload.get("persisted")),
        }

    # ---------- 工具循环 ----------

    def _tool_loop(
        self,
        tools_schema: Optional[List[Dict[str, Any]]],
        token: CancelToken,
        *,
        turn_id: str,
        request_system_message: Optional[Dict[str, Any]],
        request_enabled_tools: frozenset[str],
    ) -> tuple[int, str, TraceCollector, List[Dict[str, Any]]]:
        """从唯一 history 驱动工具循环，并在每个协议边界立即持久化。

        provider 所需的 ``messages`` 只在一次请求开始前生成快照。普通重试复用同一
        快照；assistant、tool 和运行时证据一旦完整产生，就先写 journal，再进入
        下一次请求。这样不存在回合末尾反向提取或二次提交。
        """

        last_answer = ""
        trace_collector = TraceCollector()
        loop_compactions: List[Dict[str, Any]] = []
        max_rounds = self.max_tool_rounds

        def _append_assistant(
            raw_answer: str,
            *,
            tool_calls: Optional[List[Dict[str, Any]]] = None,
            reasoning_content: Optional[str] = None,
        ) -> None:
            """保存 provider 的完整 assistant 结果，不使用 UI 过滤后的文本。"""

            self._append_history(
                [Message.create_assistant_message(
                    input_text=raw_answer,
                    tool_calls=tool_calls,
                    reasoning_content=reasoning_content,
                )],
                turn_id=turn_id,
                event_kind="assistant",
            )

        for round_idx in range(1, max_rounds + 1):
            if token.is_cancelled():
                final_round_idx = round_idx - 1 if round_idx > 1 else 1
                self.event_bus.emit(RoundEnd(
                    round_idx=final_round_idx,
                    has_tool_calls=False, final=True,
                ))
                return final_round_idx, last_answer, trace_collector, loop_compactions

            # 通知在真正发请求前才消费，并立即进入 canonical history。即使它只对
            # 当前工具轮有意义，也不能在下一次请求中从旧位置消失。
            self._inject_runtime_history(turn_id)

            self.event_bus.emit(RoundStart(
                round_idx=round_idx,
                max_rounds=max_rounds,
            ))

            request_messages = self._provider_request_messages(
                system_message=request_system_message,
            )
            request_tokens_est = self._estimate_request_tokens(request_messages, tools_schema)
            calibrated_tokens = self._calibrated_request_tokens(request_tokens_est)
            self._emit_context_window_update(
                reason="round_start",
                round_idx=round_idx,
                messages=request_messages,
                tools_schema=tools_schema,
                used_tokens=request_tokens_est,
            )
            if calibrated_tokens >= self._auto_compact_trigger_tokens():
                compact_event = self._mid_turn_compact(
                    round_idx=round_idx,
                    request_tokens=calibrated_tokens,
                    active_turn_id=turn_id,
                    tools_schema=tools_schema,
                    request_system_message=request_system_message,
                    request_enabled_tools=request_enabled_tools,
                )
                if compact_event is not None:
                    loop_compactions.append(compact_event)
                    request_messages = self._provider_request_messages(
                        system_message=request_system_message,
                    )
                    request_tokens_est = self._estimate_request_tokens(
                        request_messages,
                        tools_schema,
                    )
                    calibrated_tokens = self._calibrated_request_tokens(request_tokens_est)
                    self._emit_context_window_update(
                        reason="mid_turn_compact",
                        round_idx=round_idx,
                        messages=request_messages,
                        tools_schema=tools_schema,
                        used_tokens=request_tokens_est,
                    )
            if calibrated_tokens >= self._full_window_blocking_threshold():
                overflow_answer = (
                    last_answer
                    or "[上下文窗口已满] 工具循环中的请求过大，已停止本轮。"
                )
                self.event_bus.emit(Error(
                    where="session",
                    message=(
                        f"context window would overflow in tool loop "
                        f"(request {calibrated_tokens} tokens)"
                    ),
                    round_idx=round_idx,
                ))
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx,
                    has_tool_calls=False,
                    final=True,
                ))
                return (
                    round_idx,
                    overflow_answer,
                    trace_collector,
                    loop_compactions,
                )
            logger.info(
                "round start: round=%s messages=%s request_tokens_est=%s calibrated_tokens=%s",
                round_idx,
                len(request_messages),
                request_tokens_est,
                calibrated_tokens,
            )
            if self.message_logger is not None:
                try:
                    self.message_logger.log(
                        sanitize_multimodal_payload(request_messages),
                        tools=tools_schema,
                        label=f"第 {round_idx} 轮 think 前",
                    )
                except Exception:
                    logger.exception("message_logger 写入失败")

            # Plan Mode: 用 _PlanParsingEventBus 代理替换真实 event_bus。
            # 这样 LLM 的流式输出 TextDelta 会被实时解析，<proposed_plan> 块内的
            # 文本自动路由为 PlanDelta 事件，块外文本继续走正常 TextDelta。
            plan_bus = (
                _PlanParsingEventBus(self)
                if self.collaboration_mode() == "plan"
                else None
            )
            overflow_retried = False
            rate_limit_retried = False
            transport_retried = False
            while True:
                self._request_token_estimates[round_idx] = request_tokens_est
                try:
                    result = self.llm.think(
                        request_messages,
                        tools=tools_schema,
                        event_bus=plan_bus if plan_bus is not None else self.event_bus,
                        cancel_event=token.event,
                        round_idx=round_idx,
                    )
                    break
                except LLMContextOverflowError as exc:
                    if overflow_retried:
                        exc.round_idx = round_idx
                        raise
                    compact_event = self._mid_turn_compact(
                        round_idx=round_idx,
                        request_tokens=calibrated_tokens,
                        active_turn_id=turn_id,
                        tools_schema=tools_schema,
                        request_system_message=request_system_message,
                        request_enabled_tools=request_enabled_tools,
                    )
                    if compact_event is None:
                        exc.round_idx = round_idx
                        raise
                    loop_compactions.append(compact_event)
                    overflow_retried = True
                    # overflow compact 是正式 replacement，成功后才生成新快照。
                    request_messages = self._provider_request_messages(
                        system_message=request_system_message,
                    )
                    request_tokens_est = self._estimate_request_tokens(
                        request_messages,
                        tools_schema,
                    )
                    calibrated_tokens = self._calibrated_request_tokens(request_tokens_est)
                except LLMRateLimitError as exc:
                    if rate_limit_retried:
                        exc.round_idx = round_idx
                        raise
                    rate_limit_retried = True
                    time.sleep(min(
                        2.0,
                        max(0.2, float(exc.details.get("retry_after") or 0.5)),
                    ))
                    # 限流重试必须复用同一个 request_messages 对象快照。
                except LLMTransportError as exc:
                    if transport_retried or (exc.partial_answer or "").strip():
                        exc.round_idx = round_idx
                        raise
                    transport_retried = True
                    # 没有产生正文时允许一次幂等重试，仍复用原请求快照。
                except LLMRequestError as exc:
                    exc.round_idx = round_idx
                    raise

            # 流式结束后，flush 解析器缓冲区中残留的计划块内容
            if plan_bus is not None:
                plan_bus.finish(round_idx)
            if self.message_logger is not None:
                try:
                    logged_messages = sanitize_multimodal_payload(request_messages)
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
                raw_answer = str(result[0] or "")
                visible_answer = self._handle_plan_blocks_in_answer(
                    raw_answer,
                    round_idx=round_idx,
                    plan_bus=plan_bus,
                )
                if token.is_cancelled():
                    # 非 FC 流同样可能在读取中途被取消，部分文本只能给 UI 展示，
                    # 不能写成一个已经完整结束的 assistant 协议项。
                    self.event_bus.emit(RoundEnd(
                        round_idx=round_idx, has_tool_calls=False, final=True,
                    ))
                    return round_idx, visible_answer, trace_collector, loop_compactions
                _append_assistant(raw_answer)
                logger.info(
                    "round final without tools: round=%s answer_chars=%s",
                    round_idx,
                    len(visible_answer),
                )
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, visible_answer, trace_collector, loop_compactions

            if not isinstance(result, dict):
                # think() 已不再返回 None；非 dict/list 视为实现错误，上抛不提交回合。
                raise LLMInvalidRequestError(
                    message=f"模型返回非预期结构: {type(result).__name__}",
                    provider=str(getattr(self.llm, "provider", "") or ""),
                    model_key=str(getattr(self.llm, "current_model_key", "") or ""),
                    model_id=str(getattr(self.llm, "model", "") or ""),
                    round_idx=round_idx,
                    retryable=False,
                )

            raw_answer = str(result.get("answer", "") or "")
            visible_answer = self._handle_plan_blocks_in_answer(
                raw_answer,
                round_idx=round_idx,
                plan_bus=plan_bus,
            )
            tool_calls = result.get("tool_calls") or []
            reasoning = result.get("reasoning_content")
            if visible_answer:
                last_answer = visible_answer

            if token.is_cancelled():
                # 未完成的流式响应不是 provider 的完整协议项，不能伪装成已完成
                # assistant 写回 history。可见部分只作为本次 UI 返回值。
                logger.info(
                    "round cancelled after llm stream: round=%s answer_chars=%s",
                    round_idx,
                    len(visible_answer),
                )
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, visible_answer, trace_collector, loop_compactions

            if not tool_calls:
                _append_assistant(
                    raw_answer,
                    reasoning_content=reasoning,
                )
                logger.info(
                    "round final: round=%s answer_chars=%s",
                    round_idx,
                    len(visible_answer),
                )
                self.event_bus.emit(RoundEnd(
                    round_idx=round_idx, has_tool_calls=False, final=True,
                ))
                return round_idx, visible_answer, trace_collector, loop_compactions

            # 工具执行前先持久化 assistant.tool_calls。若 journal 写失败，工具不会
            # 启动；进程崩溃后恢复器会为未配对调用补明确失败结果。
            _append_assistant(
                raw_answer,
                tool_calls=tool_calls,
                reasoning_content=reasoning,
            )
            current_messages = self._provider_request_messages(
                allow_pending_tool_tail=True,
                system_message=request_system_message,
            )
            self._emit_context_window_update(
                reason="tool_calls_planned",
                round_idx=round_idx,
                messages=current_messages,
                tools_schema=tools_schema,
            )
            tool_names = [
                call.get("function", {}).get("name", "?")
                for call in tool_calls
            ]
            logger.info(
                "round planned tool calls: round=%s count=%s tools=%s answer_chars=%s reasoning_chars=%s",
                round_idx,
                len(tool_calls),
                tool_names,
                len(raw_answer),
                len(reasoning or ""),
            )

            def _checkpoint_tool_result(exec_result) -> None:
                """把单个工具终态写入 journal，供进程中断后的协议恢复。"""

                if self._history_journal is None:
                    return
                tool_content = (
                    exec_result.result
                    if isinstance(exec_result.result, str)
                    else str(exec_result.result)
                )
                self._history_journal.checkpoint_tool_result(
                    self.history,
                    Message.create_tool_message(
                        tool_call_id=str(exec_result.call_id or ""),
                        tool_name=str(exec_result.name),
                        tool_output=tool_content,
                        is_error=exec_result.is_error,
                    ),
                    turn_id=turn_id,
                )

            results = self.executor.execute(
                tool_calls,
                round_idx=round_idx,
                cancel_token=token,
                execution_policy=self.tool_execution_policy or self._plan_execution_policy(),
                result_callback=_checkpoint_tool_result,
                turn_id=turn_id,
            )
            tool_messages: List[Message] = []
            for call, exec_result in zip(tool_calls, results):
                tool_content = (
                    exec_result.result
                    if isinstance(exec_result.result, str)
                    else str(exec_result.result)
                )
                tool_messages.append(Message.create_tool_message(
                    tool_call_id=str(call.get("id") or exec_result.call_id or ""),
                    tool_name=str(exec_result.name),
                    tool_output=tool_content,
                    is_error=exec_result.is_error,
                ))
                trace_collector.add_tool_result(
                    call=call,
                    name=exec_result.name,
                    result=exec_result.result,
                    is_error=exec_result.is_error,
                    round_idx=round_idx,
                )

            # 一批工具结果必须按模型声明顺序原子追加，避免并行完成顺序改变协议。
            self._append_history(
                tool_messages,
                turn_id=turn_id,
                event_kind="tool_results",
            )
            self._append_pending_images_to_history(turn_id)
            current_messages = self._provider_request_messages(
                system_message=request_system_message,
            )
            self._emit_context_window_update(
                reason="tool_result",
                round_idx=round_idx,
                messages=current_messages,
                tools_schema=tools_schema,
            )

            if self.message_logger is not None:
                try:
                    self.message_logger.log(
                        sanitize_multimodal_payload(current_messages),
                        tools=tools_schema,
                        label=f"round {round_idx} after tool results",
                    )
                except Exception:
                    logger.exception("message_logger write failed")

            self.event_bus.emit(RoundEnd(
                round_idx=round_idx, has_tool_calls=True, final=False,
            ))
            self._emit_context_window_update(
                reason="round_end",
                round_idx=round_idx,
                messages=current_messages,
                tools_schema=tools_schema,
            )
            logger.info("round end with tools: round=%s tool_results=%s", round_idx, len(results))

        # 超出最大轮数
        self.event_bus.emit(Error(
            where="session",
            message=f"工具调用超过 {max_rounds} 轮，强制终止",
            round_idx=max_rounds,
        ))
        max_rounds_answer = "（工具调用次数过多，已终止本轮）"
        self._append_history(
            [Message(
                role=MessageRole.ASSISTANT,
                content=max_rounds_answer,
                metadata={"kind": "turn_failed", "reason": "max_tool_rounds"},
            )],
            turn_id=turn_id,
            event_kind="turn_failed",
        )
        return (
            max_rounds,
            max_rounds_answer,
            trace_collector,
            loop_compactions,
        )

    # ---------- 辅助 ----------

    def _append_pending_images_to_history(self, turn_id: str) -> None:
        """把 ``load_image`` 产生的图片桥接消息正式追加到唯一 history。

        load_image 工具在视觉模型下不能用返回值带图（role=tool 不接受 image_url），
        因而仍需合成 user 消息。关键变化是：它不能只存在于临时请求数组，否则
        下一工具轮或下一用户回合会失去模型已经看过的图片证据。
        """
        try:
            from tools.tools.pending_images import drain_images
            pending = drain_images()
        except Exception:
            logger.exception("drain pending images 失败，已忽略")
            return
        if not pending:
            return

        content: List[Dict[str, Any]] = []
        for item in pending:
            image_part = item.get("image_part")
            if not isinstance(image_part, dict):
                continue
            file_name = item.get("file_name") or "image"
            content.append({"type": "text", "text": f"图片加载成功：{file_name}"})
            content.append(image_part)
        if not content:
            return

        self._append_history(
            [Message(
                role=MessageRole.USER,
                content=content,
                metadata={
                    "kind": "tool_image_bridge",
                    "tool_call_ids": [
                        str(item.get("call_id") or "")
                        for item in pending
                        if item.get("call_id")
                    ],
                },
            )],
            turn_id=turn_id,
            event_kind="tool_images",
        )
        logger.info("injected %s pending image(s) as user message", len(pending))

    @staticmethod
    def _format_llm_request_error(exc: LLMRequestError) -> str:
        """把 provider 错误渲染成用户可见短文案，不泄露密钥。"""

        kind = {
            LLMContextOverflowError: "上下文超限",
            LLMRateLimitError: "请求限流",
            LLMTransportError: "网络/传输错误",
            LLMInvalidRequestError: "请求无效",
        }.get(type(exc), "请求失败")
        status = f" HTTP {exc.status_code}" if exc.status_code else ""
        model = f" model={exc.model_id}" if exc.model_id else ""
        detail = str(exc.message or "").strip() or type(exc).__name__
        if len(detail) > 240:
            detail = detail[:240] + "…"
        return f"[LLM {kind}{status}{model}] {detail}"

    def _make_work_record(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_collector: TraceCollector,
    ):
        """把本轮工具轨迹转换成只供 state.json 使用的结构化索引。"""
        if not trace_collector.entries:
            return None
        return self.trace_state_indexer.summarize(
            user_query=user_query,
            final_answer=final_answer,
            trace_entries=trace_collector.entries,
        )

    def _auto_update_memory_and_knowledge(
        self,
        *,
        user_query: str,
        final_answer: str,
        work_record_text: str = "",
    ) -> None:
        """尽力更新长期记忆与结构化知识，不影响主回合结果。"""
        if not self.memory_writeback_enabled:
            return
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

    def _prepend_runtime_notifications(self, user_query: str) -> str:
        # Bash 后台任务沿用旧的一次性前缀；Subagent 增量只在真正 think 前消费，
        # 避免 hook 拦截或预检失败时提前推进事件游标并丢失通知。
        return self._prepend_background_notifications(user_query)

    def _inject_runtime_history(self, turn_id: str) -> None:
        """消费父任务进度和邮箱消息，并把模型可见原文追加到 history。

        通知是某一时刻发生过的回合证据，不属于 world-state baseline；但只要下一次
        provider 请求会看到它，就必须先持久化，不能在后续请求中悄悄消失。
        """

        parts: List[str] = []
        if self.subagent_task_registry is not None:
            try:
                updates = self.subagent_task_registry.drain_parent_updates(
                    self.current_runtime_session_id()
                )
                if updates:
                    parts.append(updates)
            except Exception:
                logger.exception("注入子代理运行通知失败")
        if self.runtime_message_provider is not None:
            try:
                provided = self.runtime_message_provider()
                if isinstance(provided, str) and provided.strip():
                    parts.append(provided.strip())
                elif isinstance(provided, (list, tuple)):
                    text_items = [str(item).strip() for item in provided if str(item).strip()]
                    if text_items:
                        parts.append(
                            "[父 Agent 补充指令]\n" + "\n\n".join(text_items)
                        )
            except Exception:
                logger.exception("读取运行中补充消息失败")
        if parts:
            self._append_history(
                [_make_context_evidence_message(
                    "runtime_update",
                    "\n\n".join(parts),
                )],
                turn_id=turn_id,
                event_kind="runtime_update",
            )

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
                skill_budget = max(3000, min(16000, int(self._model_max_tokens() * 0.02 * 4)))
                overview = self.skill_manager.build_skills_overview(max_chars=skill_budget)
                if overview:
                    parts.append("")
                    parts.append(overview)
            except Exception:
                logger.exception("skill overview 构建失败")

        return "\n".join(parts)


__all__ = ["AgentSession"]
