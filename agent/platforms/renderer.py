"""把 cb-agent EventBus 事件渲染成通讯软件消息。

TUI 可以直接渲染结构化事件；QQ、微信这类 IM 平台只能收文本、图片、文件等消息。
这个渲染器负责做“事件降级”：关键交互事件会发给用户，不适合 IM 的事件默认只记日志。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from agent.event_bus import EventBus
from agent.events import (
    AskUserQuestion,
    AskUserQuestionAnswered,
    BackgroundNotification,
    BuddyUpdated,
    Cancelled,
    Done,
    Error,
    Event,
    MCPStatus,
    ReasoningDelta,
    RoundEnd,
    RoundStart,
    TextDelta,
    TodoListUpdated,
    TokenUsage,
    ToolCallPlanned,
    ToolComplete,
    ToolStart,
)
from agent.question_registry import QuestionRegistry
from agent.platforms.context import get_current_platform_conversation, get_current_platform_sender
from agent.platforms.messages import ConversationKey, OutboundMessage, OutboundSegment

logger = logging.getLogger(__name__)


@dataclass
class PendingPlatformQuestion:
    """IM 端等待用户编号回复的问题映射。"""

    conversation: ConversationKey
    question_id: str
    labels: List[str]
    requester_id: Optional[str] = None
    multi_select: bool = False


class PlatformEventRenderer:
    """将 Agent 事件流转换成通讯软件可发送的消息。

    EventBus 是同步回调，可能从 LLM 线程、工具线程发事件。因此 ``send`` 必须是线程安全
    的轻量函数：实际异步发送由平台适配器自己投递到事件循环。
    """

    def __init__(
        self,
        *,
        event_bus: EventBus,
        send: Callable[[OutboundMessage], None],
        verbosity: Optional[str] = None,
        confirm_question_answer: Optional[bool] = None,
    ) -> None:
        self._bus = event_bus
        self._send = send
        self._verbosity = (verbosity or os.getenv("IM_EVENT_VERBOSITY") or "normal").strip().lower()
        self._confirm_question_answer = (
            _env_bool("IM_CONFIRM_QUESTION_ANSWER", default=True)
            if confirm_question_answer is None
            else bool(confirm_question_answer)
        )
        self._lock = threading.RLock()
        self._active_conversation: Optional[ConversationKey] = None
        self._active_sender_id: Optional[str] = None
        self._pending_by_conversation: Dict[str, PendingPlatformQuestion] = {}
        self._pending_by_question: Dict[str, PendingPlatformQuestion] = {}
        self._subscription = self._bus.subscribe(self._on_event)

    def close(self) -> None:
        self._bus.unsubscribe(self._subscription)

    def begin_run(self, conversation: ConversationKey, sender_id: Optional[str] = None) -> None:
        with self._lock:
            self._active_conversation = conversation
            self._active_sender_id = str(sender_id) if sender_id is not None else None

    def end_run(self, conversation: ConversationKey) -> None:
        with self._lock:
            if self._active_conversation == conversation:
                self._active_conversation = None
                self._active_sender_id = None

    def has_pending_question(self, conversation: ConversationKey) -> bool:
        with self._lock:
            return conversation.stable_id in self._pending_by_conversation

    def try_answer_pending(
        self,
        *,
        conversation: ConversationKey,
        text: str,
        registry: QuestionRegistry,
        sender_id: Optional[str] = None,
    ) -> bool:
        """尝试把 IM 用户消息解释成 AskUserQuestion 的回答。

        返回 True 表示这条消息已经被消费，不应再启动新的 Agent 对话。
        """

        with self._lock:
            pending = self._pending_by_conversation.get(conversation.stable_id)
        if pending is None:
            return False

        if (
            pending.requester_id
            and sender_id is not None
            and str(sender_id) != pending.requester_id
        ):
            if _looks_like_question_reply(text):
                self._emit_text(
                    conversation,
                    "只有发起这次请求的用户可以确认该操作。",
                    reason="question_unauthorized_reply",
                    kind="status",
                )
                return True
            return False

        parsed = _parse_question_reply(text, pending)
        if parsed is None:
            self._emit_text(
                conversation,
                "请按提示回复选项编号，例如 1 或 1,3；也可以回复“取消”或“其他: 你的补充”。",
                reason="question_invalid_reply",
                kind="status",
            )
            return True

        delivered = registry.submit_answer(
            pending.question_id,
            selected_labels=parsed["labels"],
            other_text=parsed.get("other_text"),
            cancelled=bool(parsed.get("cancelled")),
        )
        if not delivered:
            self._remove_pending(pending.question_id)
            self._emit_text(conversation, "这个问题已经失效，请重新发送你的请求。", reason="question_expired", kind="status")
        return True

    # ---------- EventBus 回调 ----------

    def _on_event(self, event: Event) -> None:
        if isinstance(event, (TextDelta, ReasoningDelta)):
            return

        if isinstance(event, AskUserQuestion):
            self._on_ask_user_question(event)
            return
        if isinstance(event, AskUserQuestionAnswered):
            self._on_ask_user_question_answered(event)
            return

        conversation = self._current_conversation()
        if conversation is None:
            return

        if isinstance(event, Done):
            if event.final_answer:
                self._emit_text(conversation, event.final_answer, reason="done")
            return
        if isinstance(event, TodoListUpdated):
            self._emit_text(conversation, _format_todo_items(event.items), reason="todo", kind="todo")
            return
        if isinstance(event, Error):
            self._emit_text(conversation, f"发生错误：{event.message}", reason="error", kind="status")
            return
        if isinstance(event, Cancelled):
            self._emit_text(conversation, "当前回复已取消。", reason="cancelled", kind="status")
            return
        if isinstance(event, BackgroundNotification):
            self._emit_text(
                conversation,
                f"后台任务 {event.task_id} 已结束：{event.status}，输出：{event.output_path}",
                reason="background",
                kind="status",
            )
            return
        if isinstance(event, ToolStart):
            self._emit_text(conversation, _format_tool_start(event), reason="tool_start", kind="status")
            return
        if isinstance(event, ToolComplete) and event.name == "send_message_asset":
            self._on_asset_tool_complete(conversation, event)
            return

        if self._verbosity == "full":
            text = _format_full_event(event)
            if text:
                self._emit_text(conversation, text, reason="event_full", kind="status")

    def _on_ask_user_question(self, event: AskUserQuestion) -> None:
        conversation = self._current_conversation()
        if conversation is None:
            logger.warning("AskUserQuestion emitted without active platform conversation: %s", event.question_id)
            return
        labels = [str(item.get("label") or "") for item in event.options]
        pending = PendingPlatformQuestion(
            conversation=conversation,
            question_id=event.question_id,
            labels=labels,
            requester_id=self._current_sender_id(),
            multi_select=bool(event.multi_select),
        )
        with self._lock:
            old = self._pending_by_conversation.get(conversation.stable_id)
            if old is not None:
                self._pending_by_question.pop(old.question_id, None)
            self._pending_by_conversation[conversation.stable_id] = pending
            self._pending_by_question[event.question_id] = pending
        self._send(OutboundMessage(
            conversation=conversation,
            segments=[OutboundSegment.text_segment(_format_question(event), kind="question")],
            reason="ask_user_question",
        ))

    def _on_ask_user_question_answered(self, event: AskUserQuestionAnswered) -> None:
        pending = self._remove_pending(event.question_id)
        if pending is None or not self._confirm_question_answer:
            return
        if event.cancelled:
            text = "已取消选择。"
        elif event.other_text:
            text = f"已收到补充：{event.other_text}"
        else:
            text = "已选择：" + "、".join(event.selected_labels)
        self._emit_text(pending.conversation, text, reason="question_answered", kind="status")

    def _on_asset_tool_complete(self, conversation: ConversationKey, event: ToolComplete) -> None:
        try:
            payload = json.loads(event.result)
        except Exception:
            logger.warning("send_message_asset returned non-json result: %s", event.result[:200])
            return
        if not isinstance(payload, dict) or not payload.get("queued"):
            return
        kind = str(payload.get("kind") or "file")
        path = str(payload.get("path") or "")
        if not path:
            return
        segments: List[OutboundSegment] = []
        caption = str(payload.get("caption") or "")
        if caption:
            segments.append(OutboundSegment.text_segment(caption))
        segments.append(OutboundSegment.file_segment(
            kind=kind,
            path=path,
            file_name=str(payload.get("file_name") or ""),
            metadata=payload,
        ))
        self._send(OutboundMessage(conversation=conversation, segments=segments, reason="asset"))

    # ---------- helpers ----------

    def _current_conversation(self) -> Optional[ConversationKey]:
        # QQ/微信这类通讯平台可能同时处理多个会话。并发路径下，AgentSession
        # 会通过 ContextVar 绑定当前 ConversationKey；没有绑定时再回退到
        # begin_run/end_run 维护的串行 active 会话，兼容现有单测和 CLI 风格调用。
        current = get_current_platform_conversation()
        if current is not None:
            return current
        with self._lock:
            return self._active_conversation

    def _current_sender_id(self) -> Optional[str]:
        current = get_current_platform_sender()
        if current is not None:
            return str(current)
        with self._lock:
            return self._active_sender_id

    def _remove_pending(self, question_id: str) -> Optional[PendingPlatformQuestion]:
        with self._lock:
            pending = self._pending_by_question.pop(question_id, None)
            if pending is not None:
                self._pending_by_conversation.pop(pending.conversation.stable_id, None)
            return pending

    def _emit_text(self, conversation: ConversationKey, text: str, *, reason: str, kind: str = "text") -> None:
        clean = str(text or "").strip()
        if not clean:
            return
        self._send(OutboundMessage.text(conversation, clean, reason=reason, kind=kind))


def _format_question(event: AskUserQuestion) -> str:
    lines = [event.question.strip(), "", "请选择："]
    for idx, item in enumerate(event.options, start=1):
        label = str(item.get("label") or "").strip()
        desc = str(item.get("description") or "").strip()
        rec = "（推荐）" if event.recommended_index == idx - 1 else ""
        if desc:
            lines.append(f"{idx}. {label}{rec} - {desc}")
        else:
            lines.append(f"{idx}. {label}{rec}")
    if event.multi_select:
        lines.append("请回复编号，可多选，例如 1,3。回复“取消”可取消，“其他: 内容”可补充。")
    else:
        lines.append("请回复编号，例如 1。回复“取消”可取消，“其他: 内容”可补充。")
    return "\n".join(lines)


def _parse_question_reply(text: str, pending: PendingPlatformQuestion) -> Optional[Dict[str, object]]:
    value = str(text or "").strip()
    if not value:
        return None
    if value in {"取消", "cancel", "Cancel", "CANCEL"}:
        return {"labels": [], "cancelled": True}
    lowered = value.lower()
    for prefix in ("其他:", "其他：", "other:", "other："):
        if lowered.startswith(prefix.lower()):
            other = value[len(prefix):].strip()
            return {"labels": ["Other"], "other_text": other}

    raw_parts = [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
    if not raw_parts:
        return None
    labels: List[str] = []
    for part in raw_parts:
        if not part.isdigit():
            return None
        idx = int(part)
        if idx < 1 or idx > len(pending.labels):
            return None
        label = pending.labels[idx - 1]
        if label not in labels:
            labels.append(label)
    if len(labels) > 1 and not pending.multi_select:
        return None
    return {"labels": labels}


def _looks_like_question_reply(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value in {"取消", "cancel", "Cancel", "CANCEL"}:
        return True
    lowered = value.lower()
    if any(lowered.startswith(prefix.lower()) for prefix in ("其他:", "其他：", "other:", "other：")):
        return True
    raw_parts = [p.strip() for p in value.replace("，", ",").split(",") if p.strip()]
    return bool(raw_parts) and all(part.isdigit() for part in raw_parts)


def _format_todo_items(items: List[Dict[str, str]]) -> str:
    if not items:
        return "待办列表已清空。"
    markers = {
        "completed": "[x]",
        "in_progress": "[>]",
        "pending": "[ ]",
        "cancelled": "[~]",
    }
    lines = ["任务列表更新："]
    for item in items[:12]:
        status = str(item.get("status") or "pending")
        lines.append(f"{markers.get(status, '[?]')} {item.get('id')}. {item.get('content')} ({status})")
    if len(items) > 12:
        lines.append(f"... 还有 {len(items) - 12} 项")
    return "\n".join(lines)


def _format_tool_start(event: ToolStart) -> str:
    """把工具开始事件降级成 IM 中的一行状态消息。

    QQ/微信没有 TUI 的工具卡片，所以每次工具开始时发一条短提示。参数只做预览：
    既让用户知道 agent 正在干什么，又避免把超长文件内容或 token 泄露到群聊里。
    """

    if event.name == "bash":
        command = str(event.arguments.get("command") or "").strip()
        return f"（执行命令:{_clip_text(command or '<空命令>', 700)}）"
    args = _format_tool_arguments(event.arguments)
    return f"（调用工具:{event.name} {args}）"


def _format_tool_arguments(arguments: Dict[str, Any]) -> str:
    if not arguments:
        return "{}"
    safe = _sanitize_argument_value(arguments)
    try:
        text = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except Exception:
        text = str(safe)
    return _clip_text(text, 900)


def _sanitize_argument_value(value: Any, *, depth: int = 0, key: str = "") -> Any:
    """生成适合发到通讯软件的参数预览。

    这里只影响 QQ/微信里的状态提示，不改变实际工具入参。敏感键名脱敏，长字符串、
    长列表和深层对象截断，避免 IM 消息泄露凭据或刷屏。
    """

    lowered_key = key.lower()
    if any(token in lowered_key for token in ("key", "token", "secret", "password", "authorization", "cookie")):
        return "<已脱敏>"
    if depth >= 4:
        return "<嵌套过深，已省略>"
    if isinstance(value, dict):
        items = list(value.items())
        result: Dict[str, Any] = {}
        for item_key, item_value in items[:12]:
            text_key = str(item_key)
            result[text_key] = _sanitize_argument_value(item_value, depth=depth + 1, key=text_key)
        if len(items) > 12:
            result["..."] = f"还有 {len(items) - 12} 项"
        return result
    if isinstance(value, list):
        result = [_sanitize_argument_value(item, depth=depth + 1, key=key) for item in value[:8]]
        if len(value) > 8:
            result.append(f"... 还有 {len(value) - 8} 项")
        return result
    if isinstance(value, str):
        return _clip_text(value, 220)
    return value


def _clip_text(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [+{len(value) - limit} chars]"


def _format_full_event(event: Event) -> str:
    if isinstance(event, ToolStart):
        return f"开始调用工具：{event.name}"
    if isinstance(event, ToolComplete):
        return f"工具完成：{event.name}，耗时 {event.duration_seconds:.2f}s"
    if isinstance(event, ToolCallPlanned):
        return f"计划调用工具：{event.name}"
    if isinstance(event, RoundStart):
        return f"开始第 {event.round_idx + 1} 轮推理。"
    if isinstance(event, RoundEnd):
        return f"第 {event.round_idx + 1} 轮结束。"
    if isinstance(event, TokenUsage):
        return f"Token 用量：prompt={event.prompt_tokens}, completion={event.completion_tokens}"
    if isinstance(event, MCPStatus):
        return f"MCP 状态：{event.status}，connected={event.connected}/{event.total}"
    if isinstance(event, BuddyUpdated):
        state = event.state if isinstance(event.state, dict) else {}
        return f"Buddy 状态更新：{state.get('status', 'unknown')}"
    return ""


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


__all__ = ["PlatformEventRenderer", "PendingPlatformQuestion"]
