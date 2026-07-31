"""工具轨迹提取与轻量会话状态存储。

模型可见历史由 ``HistoryJournal`` 独占管理。本模块不保存对话消息，也不维护
pending、active turn、transcript 或 compact offset；它只保存 UI/工作索引和
累计用量。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

TRACE_RESULT_LIMIT = 100
FILE_SUMMARY_LIMIT = 300
RECENT_COMMANDS_LIMIT = 10


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int) -> str:
    """把任意值压成适合 state.json 的单行短文本。"""

    if text is None:
        return ""
    value = str(text).replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\s+", " ", value).strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)].rstrip() + "…"


def _json_loads_maybe(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return raw


def _tool_name(call: Dict[str, Any]) -> str:
    return str((call.get("function") or {}).get("name") or "")


def _tool_arguments(call: Dict[str, Any]) -> Dict[str, Any]:
    parsed = _json_loads_maybe((call.get("function") or {}).get("arguments", "{}"))
    return parsed if isinstance(parsed, dict) else {}


def _summarize_arguments(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """只保留结构化索引需要的工具参数，正文仍只存在 canonical history。"""

    if name == "file_write":
        keys = ("path",)
    elif name == "file_edit":
        keys = ("path", "replace_all")
    elif name == "file_read":
        keys = (
            "path",
            "head",
            "tail",
            "start_line",
            "end_line",
            "start_char",
            "end_char",
        )
    elif name == "bash":
        keys = ("command", "cwd", "timeout", "background")
    else:
        keys = tuple(
            key
            for key in arguments
            if key not in {"content", "stdout", "stderr", "result"}
        )
    return {key: _clip(arguments.get(key), 160) for key in keys if key in arguments}


@dataclass
class TraceEntry:
    """单个工具结果的短索引，不参与模型上下文。"""

    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    result_summary: str = ""
    is_error: bool = False
    round_idx: int = 0
    timestamp: str = field(default_factory=_now_iso)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkRecord:
    """一轮结束后用于更新 state.json 的结构化工作索引。"""

    files_seen: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    files_modified: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    recent_commands: List[Dict[str, Any]] = field(default_factory=list)


class TraceStateIndexer:
    """从工具轨迹提取文件和命令索引，不生成第二份对话摘要。"""

    def summarize(
        self,
        *,
        user_query: str,
        final_answer: str,
        trace_entries: Sequence[TraceEntry],
    ) -> WorkRecord:
        del user_query, final_answer
        files_seen: Dict[str, Dict[str, Any]] = {}
        files_modified: Dict[str, Dict[str, Any]] = {}
        recent_commands: List[Dict[str, Any]] = []
        for entry in trace_entries:
            metadata = entry.metadata
            if entry.name == "file_read":
                path = str(metadata.get("path") or entry.arguments.get("path") or "")
                if path:
                    files_seen[path] = {
                        "last_mode": metadata.get("mode"),
                        "total_lines": metadata.get("total_lines"),
                        "returned_lines": metadata.get("returned_lines"),
                        "truncated": metadata.get("truncated"),
                        "summary": _clip(
                            metadata.get("content_preview") or entry.result_summary,
                            FILE_SUMMARY_LIMIT,
                        ),
                        "last_seen_at": entry.timestamp,
                    }
            elif entry.name in {"file_write", "file_edit"}:
                path = str(metadata.get("path") or entry.arguments.get("path") or "")
                if path:
                    files_modified[path] = {
                        "lines_added": metadata.get("lines_added"),
                        "lines_removed": metadata.get("lines_removed"),
                        "summary": _clip(entry.result_summary, FILE_SUMMARY_LIMIT),
                        "last_modified_at": entry.timestamp,
                    }
            elif entry.name in {"bash", "bash_task"}:
                command = str(metadata.get("command") or entry.arguments.get("command") or "")
                recent_commands.append({
                    "command": _clip(command, 180),
                    "cwd": metadata.get("cwd"),
                    "exit_code": metadata.get("exit_code"),
                    "summary": _clip(entry.result_summary, TRACE_RESULT_LIMIT),
                    "output_file": metadata.get("output_file"),
                    "ts": entry.timestamp,
                })
        return WorkRecord(
            files_seen=files_seen,
            files_modified=files_modified,
            recent_commands=recent_commands[-RECENT_COMMANDS_LIMIT:],
        )


class TraceCollector:
    """一次 chat 内收集工具结果的短索引。"""

    def __init__(self, result_limit: int = TRACE_RESULT_LIMIT) -> None:
        self.result_limit = result_limit
        self.entries: List[TraceEntry] = []

    def add_tool_result(
        self,
        *,
        call: Dict[str, Any],
        name: str,
        result: Any,
        is_error: bool,
        round_idx: int,
    ) -> TraceEntry:
        entry = trace_entry_from_tool_result(
            name=name or _tool_name(call),
            arguments=_tool_arguments(call),
            result=result,
            is_error=is_error,
            round_idx=round_idx,
            result_limit=self.result_limit,
        )
        self.entries.append(entry)
        return entry

def trace_entry_from_tool_result(
    *,
    name: str,
    arguments: Dict[str, Any],
    result: Any,
    is_error: bool,
    round_idx: int,
    result_limit: int = TRACE_RESULT_LIMIT,
) -> TraceEntry:
    """把工具结果转换成有界 state 索引，不修改模型已经看到的 tool 消息。"""

    parsed = _json_loads_maybe(result)
    metadata: Dict[str, Any] = {}
    summary = ""
    if isinstance(parsed, dict):
        if name == "file_read":
            preview = _clip(parsed.get("content"), result_limit)
            metadata = {
                "path": parsed.get("path"),
                "mode": parsed.get("mode"),
                "total_lines": parsed.get("total_lines"),
                "returned_lines": parsed.get("returned_lines"),
                "truncated": parsed.get("truncated"),
                "content_preview": preview,
            }
            summary = _clip(parsed.get("error") or preview, result_limit)
        elif name in {"file_write", "file_edit"}:
            metadata = {
                "path": parsed.get("path"),
                "lines_added": parsed.get("lines_added"),
                "lines_removed": parsed.get("lines_removed"),
                "message": parsed.get("message"),
            }
            summary = _clip(parsed.get("error") or parsed.get("message") or parsed, result_limit)
        elif name in {"bash", "bash_task"}:
            task = parsed.get("task") if isinstance(parsed.get("task"), dict) else {}
            stdout = _clip(parsed.get("stdout") or parsed.get("content"), result_limit)
            stderr = _clip(parsed.get("stderr"), result_limit)
            metadata = {
                "command": task.get("command") or arguments.get("command"),
                "cwd": task.get("cwd") or parsed.get("cwd") or arguments.get("cwd"),
                "exit_code": task.get("exit_code") or parsed.get("exit_code"),
                "output_file": task.get("output_path") or parsed.get("output_file"),
            }
            summary = _clip(parsed.get("error") or stderr or stdout, result_limit)
        else:
            metadata = {
                key: value
                for key, value in parsed.items()
                if key not in {"content", "stdout", "stderr", "result"}
            }
            summary = _clip(
                parsed.get("error")
                or parsed.get("summary")
                or parsed.get("message")
                or parsed.get("result")
                or parsed.get("content")
                or parsed,
                result_limit,
            )
        is_error = bool(is_error or parsed.get("error"))
    else:
        summary = _clip(parsed, result_limit)
    return TraceEntry(
        name=name,
        arguments=_summarize_arguments(name, arguments),
        result_summary=summary,
        is_error=bool(is_error),
        round_idx=round_idx,
        metadata={key: value for key, value in metadata.items() if value not in (None, "")},
    )


class LocalSessionStore:
    """只保存会话索引、工作状态、usage 和 tokenizer 校准数据。"""

    def __init__(
        self,
        root: Optional[Path | str] = None,
    ) -> None:
        self.root = Path(root or Path.cwd() / ".cbagent" / "sessions")
        self.index_path = self.root / "index.json"
        self.active_session_id: Optional[str] = None
        self.state: Dict[str, Any] = {}
        self._load_or_create()

    @property
    def active_dir(self) -> Path:
        if not self.active_session_id:
            raise RuntimeError("active_session_id is not set")
        return self.root / self.active_session_id

    def ensure_active(self) -> None:
        if (
            self.active_session_id
            and self._is_valid_session_id(self.active_session_id)
            and self.active_dir.exists()
        ):
            return
        self._load_or_create()

    def _load_or_create(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        index = self._read_json(self.index_path, {})
        active = str(index.get("active_session_id") or "") if isinstance(index, dict) else ""
        if active and self._is_valid_session_id(active) and (self.root / active).is_dir():
            self.active_session_id = active
            state = self._read_json(self.active_dir / "state.json", {})
            self.state = state if isinstance(state, dict) else {}
            return
        self.create_session()

    def create_session(self) -> Dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        next_session_id = f"session_{stamp}_{uuid.uuid4().hex[:8]}"
        next_dir = self.root / next_session_id
        next_state = self._new_state(session_id=next_session_id)
        try:
            next_dir.mkdir(parents=True, exist_ok=False)
            self._write_json(next_dir / "state.json", next_state)
            self._write_json(next_dir / "usage.json", self._empty_usage())
            self._write_json(self.index_path, {
                "active_session_id": next_session_id,
                "updated_at": _now_iso(),
            })
        except Exception:
            # 新目录尚未成为进程内 active session，可以安全清理失败事务；旧会话
            # 的 active 指针和 state 仍保持原样。
            shutil.rmtree(next_dir, ignore_errors=True)
            raise
        self.active_session_id = next_session_id
        self.state = next_state
        return self.current_session_summary() or {}

    def switch_session(self, session_id: str) -> Dict[str, Any]:
        target = self.resolve_session_dir(session_id)
        state = self._read_json(target / "state.json", {})
        next_state = state if isinstance(state, dict) and state else self._new_state()
        next_state = dict(next_state)
        next_state["session_id"] = session_id

        # 先把目标状态和 active 索引完整写盘，最后再切换进程内指针。这样写盘失败
        # 时当前会话仍保持原状，不会出现 store 指向目标而 Agent history 仍是旧会话。
        self._write_json(target / "state.json", next_state)
        self._write_json(self.index_path, {
            "active_session_id": session_id,
            "updated_at": _now_iso(),
        })
        self.active_session_id = session_id
        self.state = next_state
        return self.current_session_summary() or {}

    def resolve_session_dir(self, session_id: str) -> Path:
        """只读校验会话 ID，并返回受 sessions 根目录约束的目标目录。"""

        if not self._is_valid_session_id(session_id):
            raise ValueError(f"invalid session_id: {session_id!r}")
        target = self.root / session_id
        if not target.is_dir():
            raise ValueError(f"session not found: {session_id}")
        return target

    def list_sessions(self) -> List[Dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True)
        sessions = [
            self._session_summary(child)
            for child in self.root.iterdir()
            if child.is_dir() and self._is_valid_session_id(child.name)
        ]
        sessions.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return sessions

    def current_session_summary(self) -> Optional[Dict[str, Any]]:
        if not self.active_session_id:
            return None
        target = self.root / self.active_session_id
        return self._session_summary(target) if target.is_dir() else None

    @staticmethod
    def _empty_usage() -> Dict[str, Any]:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_prompt_tokens": 0,
            "cache_miss_tokens": 0,
            "requests": 0,
            "updated_at": _now_iso(),
        }

    def load_usage(self) -> Dict[str, Any]:
        if not self.active_session_id:
            return self._empty_usage()
        raw = self._read_json(self.active_dir / "usage.json", {})
        usage = self._empty_usage()
        if isinstance(raw, dict):
            for key in (
                "prompt_tokens",
                "completion_tokens",
                "cached_prompt_tokens",
                "cache_miss_tokens",
                "requests",
            ):
                usage[key] = max(0, int(raw.get(key) or 0))
            usage["updated_at"] = str(raw.get("updated_at") or usage["updated_at"])
        return usage

    def add_token_usage(self, event: Any) -> Dict[str, Any]:
        self.ensure_active()
        usage = self.load_usage()
        prompt = max(0, int(getattr(event, "prompt_tokens", 0) or 0))
        completion = max(0, int(getattr(event, "completion_tokens", 0) or 0))
        cached = max(0, int(
            getattr(event, "cached_prompt_tokens", None)
            or getattr(event, "prompt_cache_hit_tokens", None)
            or 0
        ))
        explicit_miss = getattr(event, "prompt_cache_miss_tokens", None)
        miss = max(0, int(
            explicit_miss
            if explicit_miss is not None
            else max(0, prompt - cached)
        ))
        usage["prompt_tokens"] += prompt
        usage["completion_tokens"] += completion
        usage["cached_prompt_tokens"] += min(cached, prompt) if prompt else cached
        usage["cache_miss_tokens"] += miss
        usage["requests"] += 1
        usage["updated_at"] = _now_iso()
        self._write_json(self.active_dir / "usage.json", usage)
        return usage

    def load_token_calibration(self, key: str) -> Optional[float]:
        raw = self._read_json(self.root / "token-calibration.json", {})
        item = raw.get(key) if isinstance(raw, dict) else None
        try:
            return float((item or {}).get("ratio")) if isinstance(item, dict) else None
        except (TypeError, ValueError):
            return None

    def save_token_calibration(self, key: str, ratio: float, samples: int) -> None:
        path = self.root / "token-calibration.json"
        raw = self._read_json(path, {})
        data = raw if isinstance(raw, dict) else {}
        data[key] = {
            "ratio": round(float(ratio), 6),
            "samples": max(1, int(samples)),
            "updated_at": _now_iso(),
        }
        self._write_json(path, data)

    def commit_turn_state(
        self,
        *,
        user_query: str,
        work_record: Optional[WorkRecord] = None,
    ) -> None:
        self.ensure_active()
        if work_record is not None:
            self._merge_work_record(work_record, user_query=user_query)
        else:
            self._bump_turn(user_query=user_query)
        self.save_state(self.state)
        self._write_index()

    def record_compaction_state(self, *, summary: str, reason: str) -> None:
        self.ensure_active()
        state = self.state or self._new_state()
        state["updated_at"] = _now_iso()
        state["last_compact_summary"] = _clip(summary, 4000)
        state["last_compact_reason"] = str(reason or "compact")
        state["compact_count"] = int(state.get("compact_count") or 0) + 1
        self.save_state(state)
        self._write_index()

    def _merge_work_record(self, record: WorkRecord, *, user_query: str) -> None:
        self._bump_turn(user_query=user_query)
        files_seen = self.state.setdefault("files_seen", {})
        if isinstance(files_seen, dict):
            files_seen.update(record.files_seen)
        files_modified = self.state.setdefault("files_modified", {})
        if isinstance(files_modified, dict):
            files_modified.update(record.files_modified)
        commands = self.state.setdefault("recent_commands", [])
        if isinstance(commands, list):
            commands.extend(record.recent_commands)
            self.state["recent_commands"] = commands[-RECENT_COMMANDS_LIMIT:]

    def _bump_turn(self, *, user_query: str) -> None:
        state = self.state or self._new_state()
        state["updated_at"] = _now_iso()
        state["turn_count"] = int(state.get("turn_count") or 0) + 1
        if user_query:
            state["active_task"] = _clip(user_query, 200)
        self.state = state

    def save_state(self, state: Dict[str, Any]) -> None:
        self.ensure_active()
        self.state = dict(state)
        self._write_json(self.active_dir / "state.json", self.state)

    def clear_active_session(self) -> None:
        active_session_id = self.active_session_id
        target = self.root / active_session_id if active_session_id else None
        tombstone: Optional[Path] = None
        if target is not None and target.exists():
            self._assert_safe_session_dir(target)
            tombstone = self.root / f".clearing-{active_session_id}-{uuid.uuid4().hex[:8]}"
            target.rename(tombstone)
        try:
            if self.index_path.exists():
                self.index_path.unlink()
        except Exception:
            # active 目录已原子改名但索引尚未提交时，恢复原目录即可继续使用旧会话。
            if tombstone is not None and tombstone.exists() and target is not None:
                tombstone.rename(target)
            raise
        self.active_session_id = None
        self.state = {}
        if tombstone is not None:
            try:
                shutil.rmtree(tombstone)
            except Exception:
                # active 指针和可见会话目录已经提交清理；隐藏墓碑仅保留审计数据，
                # 不能让上层误以为 canonical history 仍然处于活动状态。
                logger.exception("清理会话墓碑目录失败: %s", tombstone)

    def _new_state(self, *, session_id: Optional[str] = None) -> Dict[str, Any]:
        now = _now_iso()
        return {
            "session_id": (
                self.active_session_id if session_id is None else session_id
            ),
            "project_root": str(self.root.parent.parent.resolve()),
            "created_at": now,
            "updated_at": now,
            "turn_count": 0,
            "active_task": "",
            "files_seen": {},
            "files_modified": {},
            "recent_commands": [],
        }

    def _write_index(self) -> None:
        self._write_json(self.index_path, {
            "active_session_id": self.active_session_id,
            "updated_at": _now_iso(),
        })

    def _session_summary(self, session_dir: Path) -> Dict[str, Any]:
        state = self._read_json(session_dir / "state.json", {})
        if not isinstance(state, dict):
            state = {}
        created_at = str(
            state.get("created_at")
            or datetime.fromtimestamp(session_dir.stat().st_mtime, timezone.utc).isoformat()
        )
        updated_at = str(state.get("updated_at") or self._newest_mtime(session_dir))
        return {
            "session_id": session_dir.name,
            "created_at": created_at,
            "updated_at": updated_at,
            "turn_count": int(state.get("turn_count") or 0),
            "active_task": _clip(state.get("active_task"), 120),
            "rolling_summary": _clip(state.get("last_compact_summary"), 180),
            "is_active": session_dir.name == self.active_session_id,
        }

    @staticmethod
    def _newest_mtime(session_dir: Path) -> str:
        paths = [
            session_dir,
            session_dir / "state.json",
            session_dir / "history.jsonl",
            session_dir / "usage.json",
        ]
        newest = max(
            (path.stat().st_mtime for path in paths if path.exists()),
            default=session_dir.stat().st_mtime,
        )
        return datetime.fromtimestamp(newest, timezone.utc).isoformat()

    @staticmethod
    def _is_valid_session_id(session_id: str) -> bool:
        return bool(re.fullmatch(r"session_[A-Za-z0-9_-]+", str(session_id or "")))

    def _assert_safe_session_dir(self, target: Path) -> None:
        resolved_root = self.root.resolve()
        resolved_target = target.resolve()
        if resolved_target.parent != resolved_root or not self._is_valid_session_id(target.name):
            raise RuntimeError(f"refusing to remove unsafe session path: {target}")

    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temp.replace(path)


__all__ = [
    "LocalSessionStore",
    "TraceStateIndexer",
    "TraceCollector",
    "TraceEntry",
    "WorkRecord",
    "trace_entry_from_tool_result",
]
