"""子代理定义、权限和任务状态模型。"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.cancel import CancelToken


DEFAULT_SUBAGENT_TYPE = "general"
DEFAULT_SUBAGENT_MAX_TURNS = 30
TERMINAL_TASK_STATES = {"completed", "failed", "cancelled", "orphaned"}
ACTIVE_TASK_STATES = {"queued", "running", "waiting_tool", "cancelling"}


def utc_now() -> str:
    """返回带时区的 UTC ISO 时间。"""

    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, default: str = DEFAULT_SUBAGENT_TYPE) -> str:
    """把角色名或会话名规范成可持久化的稳定标识。"""

    name = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "").strip().lower())
    return name.strip("-._") or default


def clip_text(value: Any, limit: int = 1200) -> str:
    """限制事件和快照中的文本长度，避免状态文件无限膨胀。"""

    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass(frozen=True)
class SubagentPermissionPolicy:
    """角色级权限策略。

    ``tools`` 决定模型能看到哪些工具；本策略在执行器层再次校验，防止工具注册
    或模型输出异常时越过角色边界。
    """
    # Shell权限控制
    bash_mode: str = "deny"  # deny / read_only / inherit
    workspace_write: bool = False # 是否允许写入工作目录
    external_paths: bool = False # 是否允许访问工作目录外的路径
    allow_spawn: bool = False # 是否允许 spawn 子进程
    denied_tools: Tuple[str, ...] = () # 禁止使用的工具名列表，优先级高于 SubagentDefinition.tools

    def public_dict(self) -> Dict[str, Any]:
        return {
            "bash_mode": self.bash_mode,
            "workspace_write": self.workspace_write,
            "external_paths": self.external_paths,
            "allow_spawn": self.allow_spawn,
            "denied_tools": list(self.denied_tools),
        }


@dataclass(frozen=True)
class SubagentDefinition:
    """一个可注册的子代理角色定义。"""

    name: str
    description: str
    system_prompt: str
    tools: Optional[Tuple[str, ...]] = None
    max_turns: int = DEFAULT_SUBAGENT_MAX_TURNS
    permissions: SubagentPermissionPolicy = field(default_factory=SubagentPermissionPolicy)
    source_path: Optional[str] = None #配置文件路径
    builtin: bool = False  # 是否是内置代理

    def public_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": list(self.tools) if self.tools is not None else None,
            "max_turns": self.max_turns,
            "permissions": self.permissions.public_dict(),
            "source_path": self.source_path,
            "builtin": self.builtin,
        }


@dataclass
class SubagentTask:
    """子代理任务的可持久化状态和进程内控制句柄。"""

    id: str 
    subagent_id: str
    subagent_type: str
    owner_session_id: str
    description: str
    prompt: str
    output_path: str
    snapshot_path: str
    events_path: str
    started_at: str
    run_in_background: bool = True
    status: str = "queued"
    phase: str = "queued"
    result: str = ""
    error: str = ""
    rounds_used: int = 0
    tool_uses: int = 0
    total_tokens: int = 0
    current_round: int = 0
    current_tool_name: str = ""
    current_tool_call_id: str = ""
    current_tool_arguments: Dict[str, Any] = field(default_factory=dict)
    current_tool_started_at: Optional[str] = None
    active_tool_calls: Dict[str, Dict[str, Any]] = field(default_factory=dict, repr=False)
    last_tool_name: str = ""
    last_tool_status: str = ""
    last_tool_duration: float = 0.0
    updated_at: str = field(default_factory=utc_now)
    finished_at: Optional[str] = None
    heartbeat_at: str = field(default_factory=utc_now)
    event_seq: int = 0
    parent_cursor: int = 0
    cancel_requested: bool = False
    recent_events: List[Dict[str, Any]] = field(default_factory=list)
    inbox: List[str] = field(default_factory=list)
    cancel_token: CancelToken = field(default_factory=CancelToken, repr=False)
    future: Any = field(default=None, repr=False)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATES

    def duration_seconds(self) -> Optional[float]:
        """返回已完成任务耗时；运行中任务返回当前已运行时间。"""

        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at) if self.finished_at else datetime.now(timezone.utc)
            return round((end - start).total_seconds(), 3)
        except (TypeError, ValueError):
            return None

    def to_dict(self, *, include_prompt: bool = False, include_result: bool = False) -> Dict[str, Any]:
        """生成适合工具输出和快照持久化的字典。"""

        with self.lock:
            payload: Dict[str, Any] = {
                "id": self.id,
                "subagent_id": self.subagent_id,
                "subagent_type": self.subagent_type,
                "owner_session_id": self.owner_session_id,
                "description": self.description,
                "status": self.status,
                "phase": self.phase,
                "started_at": self.started_at,
                "updated_at": self.updated_at,
                "heartbeat_at": self.heartbeat_at,
                "finished_at": self.finished_at,
                "output_path": self.output_path,
                "snapshot_path": self.snapshot_path,
                "events_path": self.events_path,
                "run_in_background": self.run_in_background,
                "rounds_used": self.rounds_used,
                "current_round": self.current_round,
                "tool_uses": self.tool_uses,
                "total_tokens": self.total_tokens,
                "current_tool": {
                    "name": self.current_tool_name,
                    "call_id": self.current_tool_call_id,
                    "arguments": dict(self.current_tool_arguments),
                    "started_at": self.current_tool_started_at,
                } if self.current_tool_name else None,
                "active_tool_count": len(self.active_tool_calls),
                "active_tools": [dict(item) for item in self.active_tool_calls.values()],
                "last_tool": {
                    "name": self.last_tool_name,
                    "status": self.last_tool_status,
                    "duration_seconds": self.last_tool_duration,
                } if self.last_tool_name else None,
                "event_seq": self.event_seq,
                "cancel_requested": self.cancel_requested,
                "duration_seconds": self.duration_seconds(),
                "result_preview": clip_text(self.result, 500),
                "error": self.error,
            }
            if include_prompt:
                payload["prompt"] = self.prompt
            if include_result:
                payload["result"] = self.result
            return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any], *, snapshot_path: Path) -> "SubagentTask":
        """从持久化快照恢复任务；控制句柄始终重新创建。"""

        task_id = str(payload.get("id") or snapshot_path.stem)
        raw_status = str(payload.get("status") or "orphaned")
        status = {
            "done": "completed",
            "killed": "cancelled",
            "canceled": "cancelled",
        }.get(raw_status, raw_status)
        subagent_type = str(payload.get("subagent_type") or DEFAULT_SUBAGENT_TYPE)
        if subagent_type == "general-purpose":
            subagent_type = DEFAULT_SUBAGENT_TYPE
        output_path = str(payload.get("output_path") or snapshot_path.with_suffix(".result.txt"))
        events_path = str(payload.get("events_path") or snapshot_path.with_suffix(".events.jsonl"))
        current_tool = payload.get("current_tool") if isinstance(payload.get("current_tool"), dict) else {}
        last_tool = payload.get("last_tool") if isinstance(payload.get("last_tool"), dict) else {}
        task = cls(
            id=task_id,
            subagent_id=str(payload.get("subagent_id") or task_id),
            subagent_type=subagent_type,
            owner_session_id=str(payload.get("owner_session_id") or "legacy-main"),
            description=str(payload.get("description") or ""),
            prompt=str(payload.get("prompt") or ""),
            output_path=output_path,
            snapshot_path=str(snapshot_path),
            events_path=events_path,
            started_at=str(payload.get("started_at") or utc_now()),
            run_in_background=bool(payload.get("run_in_background", True)),
            status=status,
            phase=str(payload.get("phase") or status),
            result=str(payload.get("result") or ""),
            error=str(payload.get("error") or ""),
            rounds_used=int(payload.get("rounds_used") or 0),
            tool_uses=int(payload.get("tool_uses") or 0),
            total_tokens=int(payload.get("total_tokens") or 0),
            current_round=int(payload.get("current_round") or 0),
            current_tool_name=str(current_tool.get("name") or ""),
            current_tool_call_id=str(current_tool.get("call_id") or ""),
            current_tool_arguments=(
                dict(current_tool.get("arguments"))
                if isinstance(current_tool.get("arguments"), dict)
                else {}
            ),
            current_tool_started_at=current_tool.get("started_at"),
            active_tool_calls={
                str(item.get("call_id") or index): dict(item)
                for index, item in enumerate(payload.get("active_tools") or [])
                if isinstance(item, dict)
            },
            last_tool_name=str(last_tool.get("name") or ""),
            last_tool_status=str(last_tool.get("status") or ""),
            last_tool_duration=float(last_tool.get("duration_seconds") or 0.0),
            updated_at=str(payload.get("updated_at") or utc_now()),
            finished_at=payload.get("finished_at"),
            heartbeat_at=str(payload.get("heartbeat_at") or payload.get("updated_at") or utc_now()),
            event_seq=int(payload.get("event_seq") or 0),
            parent_cursor=int(payload.get("parent_cursor") or 0),
            cancel_requested=bool(payload.get("cancel_requested", False)),
            recent_events=[
                dict(item)
                for item in (payload.get("recent_events") or [])
                if isinstance(item, dict)
            ],
        )
        return task


__all__ = [
    "ACTIVE_TASK_STATES",
    "DEFAULT_SUBAGENT_MAX_TURNS",
    "DEFAULT_SUBAGENT_TYPE",
    "TERMINAL_TASK_STATES",
    "SubagentDefinition",
    "SubagentPermissionPolicy",
    "SubagentTask",
    "clip_text",
    "safe_name",
    "utc_now",
]
