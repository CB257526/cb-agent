"""Subagent definitions, scoped event forwarding, and background task state."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent.cancel import CancelToken
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
    ToolComplete,
    ToolStart,
)

logger = logging.getLogger(__name__)


DEFAULT_SUBAGENT_TYPE = "general-purpose"
DEFAULT_SUBAGENT_MAX_TURNS = 30


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_name(value: str) -> str:
    name = re.sub(r"[^0-9A-Za-z_.-]+", "-", str(value or "").strip().lower())
    return name.strip("-._") or DEFAULT_SUBAGENT_TYPE


def _clip(text: Any, limit: int = 1200) -> str:
    s = "" if text is None else str(text)
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)].rstrip() + "..."


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    system_prompt: str
    tools: Optional[List[str]] = None
    max_turns: int = DEFAULT_SUBAGENT_MAX_TURNS
    source_path: Optional[str] = None

    def public_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "tools": self.tools,
            "max_turns": self.max_turns,
            "source_path": self.source_path,
        }


class SubagentRegistry:
    """Load built-in and markdown-defined subagents.

    User files are read from both project ``.cbagent/agents/*.md`` and
    ``~/.cbagent/agents/*.md``. Project definitions override global ones with
    the same name.
    """

    def __init__(self, workspace_dir: Path, user_agents_dir: Optional[Path] = None) -> None:
        self.workspace_dir = Path(workspace_dir)
        self.user_agents_dir = Path(user_agents_dir or (Path.home() / ".cbagent" / "agents"))
        self.project_agents_dir = self.workspace_dir / ".cbagent" / "agents"
        self._lock = threading.RLock()
        self._definitions: Dict[str, SubagentDefinition] = {}
        self.refresh()

    def refresh(self) -> None:
        definitions: Dict[str, SubagentDefinition] = {
            DEFAULT_SUBAGENT_TYPE: self._built_in_general()
        }
        for directory in (self.user_agents_dir, self.project_agents_dir):
            for item in self._load_dir(directory):
                definitions[item.name] = item
        with self._lock:
            self._definitions = definitions

    def get(self, name: Optional[str]) -> SubagentDefinition:
        key = _safe_name(name or DEFAULT_SUBAGENT_TYPE)
        with self._lock:
            found = self._definitions.get(key)
            if found is not None:
                return found
            return self._definitions[DEFAULT_SUBAGENT_TYPE]

    def list(self) -> List[SubagentDefinition]:
        with self._lock:
            return [self._definitions[name] for name in sorted(self._definitions)]

    def _load_dir(self, directory: Path) -> Iterable[SubagentDefinition]:
        if not directory.exists():
            return []
        out: List[SubagentDefinition] = []
        for path in sorted(directory.glob("*.md")):
            try:
                out.append(self._load_file(path))
            except Exception:
                logger.exception("failed to load subagent definition: %s", path)
        return out

    def _load_file(self, path: Path) -> SubagentDefinition:
        text = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        name = _safe_name(str(meta.get("name") or path.stem))
        description = str(meta.get("description") or _infer_description(body) or name)
        tools = _parse_tools(meta.get("tools"))
        max_turns = _parse_int(meta.get("max_turns") or meta.get("max_turns".replace("_", "-")), DEFAULT_SUBAGENT_MAX_TURNS)
        system_prompt = body.strip() or description
        return SubagentDefinition(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            max_turns=max(1, max_turns),
            source_path=str(path),
        )

    @staticmethod
    def _built_in_general() -> SubagentDefinition:
        prompt = (
            "You are a focused subagent running inside cb-agent. Work only on the delegated task. "
            "Use the available coding tools when helpful, keep your own context isolated, and return "
            "a concise report with what you did, what you found, and any remaining risks. Do not ask "
            "the user questions directly; if information is missing, state your assumption in the final report."
        )
        return SubagentDefinition(
            name=DEFAULT_SUBAGENT_TYPE,
            description="General-purpose focused worker for research, code inspection, and implementation subtasks.",
            system_prompt=prompt,
            tools=None,
            max_turns=DEFAULT_SUBAGENT_MAX_TURNS,
            source_path=None,
        )


def _split_frontmatter(text: str) -> tuple[Dict[str, Any], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}, normalized
    raw_meta = normalized[4:end]
    body = normalized[end + len("\n---\n") :]
    meta: Dict[str, Any] = {}
    for line in raw_meta.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().replace("-", "_").lower()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key] = [p.strip().strip("'\"") for p in value[1:-1].split(",") if p.strip()]
        else:
            meta[key] = value.strip("'\"")
    return meta, body


def _infer_description(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return _clip(stripped, 160)
    return ""


def _parse_tools(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",")]
    elif isinstance(value, list):
        parts = [str(p).strip() for p in value]
    else:
        return None
    names = [_safe_name(p) for p in parts if p]
    return names or None


def _parse_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ScopedEventBus:
    """Forward child-agent events without leaking child text streams as root output."""

    def __init__(
        self,
        parent_bus: EventBus,
        *,
        subagent_id: str,
        subagent_type: str,
        description: str,
        task_id: Optional[str] = None,
        run_in_background: bool = False,
        parent_session_id: Optional[str] = None,
    ) -> None:
        self.parent_bus = parent_bus
        self.subagent_id = subagent_id
        self.subagent_type = subagent_type
        self.description = description
        self.task_id = task_id
        self.run_in_background = run_in_background
        self.parent_session_id = parent_session_id
        self.final_answer = ""
        self.rounds_used = 0
        self.cancelled = False

    def emit(self, event: Any) -> None:
        if isinstance(event, HookStarted) or isinstance(event, HookCompleted):
            self.parent_bus.emit(event)
            return
        if isinstance(event, Done):
            self.final_answer = event.final_answer or ""
            self.rounds_used = int(event.rounds_used or 0)
            self.cancelled = bool(event.cancelled)
            return
        progress = self._to_progress(event)
        if progress is not None:
            self.parent_bus.emit(progress)

    def subscribe(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def unsubscribe(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def clear(self) -> None:
        return None

    @property
    def subscriber_count(self) -> int:
        return 0

    def _to_progress(self, event: Any) -> Optional[SubagentProgress]:
        message = ""
        status = "running"
        round_idx = int(getattr(event, "round_idx", 0) or 0)
        if isinstance(event, RoundStart):
            message = f"round {event.round_idx} started"
        elif isinstance(event, RoundEnd):
            message = f"round {event.round_idx} ended"
        elif isinstance(event, ToolStart):
            message = f"tool {event.name} started"
        elif isinstance(event, ToolComplete):
            status = "error" if event.is_error else "running"
            message = f"tool {event.name} completed"
        elif isinstance(event, Error):
            status = "error"
            message = f"{event.where}: {_clip(event.message, 300)}"
        elif isinstance(event, Cancelled):
            status = "cancelled"
            message = f"cancelled: {event.where}"
        else:
            return None
        return SubagentProgress(
            subagent_id=self.subagent_id,
            subagent_type=self.subagent_type,
            task_id=self.task_id,
            status=status,
            message=message,
            round_idx=round_idx,
        )


@dataclass
class SubagentTask:
    id: str
    subagent_id: str
    subagent_type: str
    description: str
    prompt: str
    output_path: str
    started_at: str
    status: str = "running"
    result: str = ""
    error: str = ""
    rounds_used: int = 0
    finished_at: Optional[str] = None
    notified: bool = False
    cancel_requested: bool = False
    cancel_token: CancelToken = field(default_factory=CancelToken, repr=False)
    thread: Optional[threading.Thread] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "subagent_id": self.subagent_id,
            "subagent_type": self.subagent_type,
            "description": self.description,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "output_path": self.output_path,
            "rounds_used": self.rounds_used,
            "result_preview": _clip(self.result, 500),
            "error": self.error,
            "cancel_requested": self.cancel_requested,
            "duration_seconds": self.duration_seconds(),
        }

    def duration_seconds(self) -> Optional[float]:
        if not self.finished_at:
            return None
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
            return round((end - start).total_seconds(), 3)
        except (TypeError, ValueError):
            return None


TaskTarget = Callable[[SubagentTask, CancelToken], Dict[str, Any]]


class SubagentTaskRegistry:
    """In-process background subagent registry with JSON output files."""

    def __init__(self, output_dir: Path) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._tasks: Dict[str, SubagentTask] = {}
        self._lock = threading.RLock()

    def spawn(
        self,
        *,
        subagent_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
        target: TaskTarget,
    ) -> SubagentTask:
        task_id = f"subagent_{uuid.uuid4().hex[:10]}"
        output_path = str((self.output_dir / f"{task_id}.json").resolve())
        task = SubagentTask(
            id=task_id,
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            description=description,
            prompt=prompt,
            output_path=output_path,
            started_at=_utc_now(),
        )
        thread = threading.Thread(
            target=self._run,
            args=(task, target),
            name=f"cb-subagent-{task_id}",
            daemon=True,
        )
        task.thread = thread
        with self._lock:
            self._tasks[task_id] = task
            self._write_task(task)
        thread.start()
        return task

    def list(self) -> List[SubagentTask]:
        with self._lock:
            return list(self._tasks.values())

    def get(self, task_id: str) -> Optional[SubagentTask]:
        with self._lock:
            return self._tasks.get(task_id)

    def wait(self, task_id: str, timeout: float = 30.0) -> Optional[SubagentTask]:
        task = self.get(task_id)
        if task is None:
            return None
        thread = task.thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=max(0.0, timeout))
        return task

    def kill(self, task_id: str) -> Optional[SubagentTask]:
        task = self.get(task_id)
        if task is None:
            return None
        if task.status == "running":
            task.cancel_requested = True
            task.status = "cancelling"
            task.cancel_token.cancel()
            self._write_task(task)
        return task

    def drain_notifications(self) -> List[SubagentTask]:
        out: List[SubagentTask] = []
        with self._lock:
            tasks = list(self._tasks.values())
            for task in tasks:
                if task.status in {"done", "failed", "killed"} and not task.notified:
                    task.notified = True
                    out.append(task)
                    self._write_task(task)
        return out

    def _run(self, task: SubagentTask, target: TaskTarget) -> None:
        try:
            result = target(task, task.cancel_token) or {}
            task.result = str(result.get("content") or "")
            task.rounds_used = int(result.get("rounds_used") or 0)
            if task.cancel_requested or task.cancel_token.is_cancelled():
                task.status = "killed"
            else:
                task.status = str(result.get("status") or "done")
        except Exception as exc:  # noqa: BLE001
            logger.exception("background subagent failed: task_id=%s", task.id)
            task.status = "failed"
            task.error = f"{type(exc).__name__}: {exc}"
        finally:
            task.finished_at = _utc_now()
            self._write_task(task)

    def _write_task(self, task: SubagentTask) -> None:
        path = Path(task.output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = task.to_dict()
        payload["result"] = task.result
        payload["prompt"] = task.prompt
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def make_subagent_started(
    *,
    subagent_id: str,
    subagent_type: str,
    description: str,
    task_id: Optional[str],
    run_in_background: bool,
    parent_session_id: Optional[str],
) -> SubagentStarted:
    return SubagentStarted(
        subagent_id=subagent_id,
        subagent_type=subagent_type,
        description=description,
        task_id=task_id,
        run_in_background=run_in_background,
        parent_session_id=parent_session_id,
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
) -> SubagentCompleted:
    return SubagentCompleted(
        subagent_id=subagent_id,
        subagent_type=subagent_type,
        description=description,
        status=status,
        content=_clip(content, 2000),
        task_id=task_id,
        output_path=output_path,
        duration_seconds=duration_seconds,
        rounds_used=rounds_used,
        is_error=is_error,
    )


__all__ = [
    "DEFAULT_SUBAGENT_TYPE",
    "DEFAULT_SUBAGENT_MAX_TURNS",
    "ScopedEventBus",
    "SubagentDefinition",
    "SubagentRegistry",
    "SubagentTask",
    "SubagentTaskRegistry",
    "make_subagent_started",
    "make_subagent_completed",
]
