"""子会话事件到任务状态和父事件总线的桥接。"""

from __future__ import annotations

from typing import Any, Optional

from agent.event_bus import EventBus
from agent.events import (
    Cancelled,
    Done,
    Error,
    HookCompleted,
    HookStarted,
    RoundEnd,
    RoundStart,
    SubagentCompleted,
    SubagentProgress,
    SubagentStarted,
    TokenUsage,
    ToolComplete,
    ToolStart,
)
from subagent.manager import SubagentTaskManager
from subagent.models import clip_text


class ScopedEventBus:
    """隔离子代理文本流，并把结构化运行事件转成父级进度。"""

    def __init__(
        self,
        parent_bus: EventBus, # 父会话事件总线
        *,
        subagent_id: str, # 子代理 ID
        subagent_type: str, # 子代理类型
        description: str, # 子代理描述
        task_id: Optional[str] = None, # 任务 ID
        run_in_background: bool = False, # 是否在后台运行
        parent_session_id: Optional[str] = None, # 父会话 ID
        task_manager: Optional[SubagentTaskManager] = None, # 子代理任务管理器
    ) -> None:
        self.parent_bus = parent_bus
        self.subagent_id = subagent_id
        self.subagent_type = subagent_type
        self.description = description
        self.task_id = task_id
        self.run_in_background = run_in_background
        self.parent_session_id = parent_session_id
        self.task_manager = task_manager
        self.final_answer = ""  # 最终回答，可能为空
        self.rounds_used = 0  # 已用轮数，可能为空
        self.cancelled = False  # 是否被取消

    def emit(self, event: Any) -> None:
        # Hook 事件已经包含 agent_scope/subagent_id，直接转发给前端即可。
        if isinstance(event, (HookStarted, HookCompleted)):
            self.parent_bus.emit(event)
            return

        managed_task = self.task_manager is not None and bool(self.task_id)
        if managed_task:
            self.task_manager.record_child_event(self.task_id, event)

        if isinstance(event, Done):
            self.final_answer = event.final_answer or ""
            self.rounds_used = int(event.rounds_used or 0)
            self.cancelled = bool(event.cancelled)
            return

        # 受管理任务由 SubagentTaskManager 的统一事件监听器负责广播。这里不再重复
        # emit；None 也可能表示任务已进入不可逆终态，绝不能走兼容回退。
        if managed_task:
            return

        # 将子代理事件转换为父代理进度事件（SubagentProgress）。
        progress = self._to_progress(event, None)
        # 只有当转换成功时才转发给父会话事件总线。供前端消费
        if progress is not None:
            self.parent_bus.emit(progress)

    # AgentSession 和 LLM 只要求 event_bus 提供同样的最小接口；子总线不维护订阅者。
    def subscribe(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def unsubscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def clear(self) -> None:
        return None

    @property
    def subscriber_count(self) -> int:
        """查看当前子代理事件总线的订阅者数量。"""
        return 0

    def _to_progress(self, event: Any, payload: Optional[dict[str, Any]]) -> Optional[SubagentProgress]:
        """将子代理事件转换为父代理进度事件。"""
        if payload is None:
            payload = _fallback_progress_payload(event)
        if payload is None:
            return None
        return SubagentProgress(
            subagent_id=self.subagent_id,
            subagent_type=self.subagent_type,
            task_id=self.task_id,
            parent_session_id=self.parent_session_id,
            status=str(payload.get("status") or "running"),
            phase=str(payload.get("phase") or "running"),
            message=str(payload.get("message") or ""),
            event_seq=int(payload.get("seq") or 0),
            round_idx=int(payload.get("round_idx") or getattr(event, "round_idx", 0) or 0),
            tool_name=str(payload.get("tool_name") or ""),
            tool_call_id=str(payload.get("tool_call_id") or ""),
            arguments_preview=payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {},
            tool_uses=int(payload.get("tool_uses") or 0),
            active_tool_count=int(payload.get("active_tool_count") or 0),
            total_tokens=int(payload.get("total_tokens") or 0),
        )


def _fallback_progress_payload(event: Any) -> Optional[dict[str, Any]]:
    """兼容没有任务管理器的测试和前台最小调用。"""

    if isinstance(event, RoundStart):
        return {"message": f"第 {event.round_idx} 轮开始", "round_idx": event.round_idx}
    if isinstance(event, RoundEnd):
        return {"message": f"第 {event.round_idx} 轮结束", "round_idx": event.round_idx}
    if isinstance(event, ToolStart):
        return {
            "message": f"工具 {event.name} 开始执行",
            "round_idx": event.round_idx,
            "tool_name": event.name,
            "tool_call_id": event.call_id,
            "arguments": event.arguments,
        }
    if isinstance(event, ToolComplete):
        return {
            "message": f"工具 {event.name} {'执行失败' if event.is_error else '执行完成'}",
            "status": "error" if event.is_error else "running",
            "round_idx": event.round_idx,
            "tool_name": event.name,
            "tool_call_id": event.call_id,
        }
    if isinstance(event, TokenUsage):
        return {
            "message": f"本轮使用 {event.total_tokens} tokens",
            "round_idx": event.round_idx,
            "total_tokens": event.total_tokens,
        }
    if isinstance(event, Error):
        return {
            "message": f"{event.where}: {clip_text(event.message, 300)}",
            "status": "error",
            "round_idx": event.round_idx,
        }
    if isinstance(event, Cancelled):
        return {
            "message": f"正在取消: {event.where}",
            "status": "cancelling",
            "round_idx": event.round_idx,
        }
    return None


def make_subagent_started(
    *,
    subagent_id: str,
    subagent_type: str,
    description: str,
    task_id: Optional[str],
    run_in_background: bool,
    parent_session_id: Optional[str],
    status: str = "running",
    phase: str = "starting",
) -> SubagentStarted:
    return SubagentStarted(
        subagent_id=subagent_id,
        subagent_type=subagent_type,
        description=description,
        task_id=task_id,
        run_in_background=run_in_background,
        parent_session_id=parent_session_id,
        status=status,
        phase=phase,
    )


def make_subagent_completed(
    *,
    subagent_id: str,
    subagent_type: str,
    description: str,
    status: str,
    content: str,
    task_id: Optional[str],
    output_path: Optional[str],
    duration_seconds: float,
    rounds_used: int,
    is_error: bool,
    parent_session_id: Optional[str] = None,
) -> SubagentCompleted:
    return SubagentCompleted(
        subagent_id=subagent_id,
        subagent_type=subagent_type,
        description=description,
        status=status,
        content=clip_text(content, 2000),
        task_id=task_id,
        parent_session_id=parent_session_id,
        output_path=output_path,
        duration_seconds=duration_seconds,
        rounds_used=rounds_used,
        is_error=is_error,
    )


__all__ = ["ScopedEventBus", "make_subagent_completed", "make_subagent_started"]
