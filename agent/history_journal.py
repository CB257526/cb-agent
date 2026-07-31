"""Canonical history v4 的追加式持久化日志。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from core.conversation_history import ConversationHistory, freeze_history_message
from core.message import Message


JOURNAL_VERSION = 4
JOURNAL_FILE_NAME = "history.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_payload(message: Message) -> dict[str, Any]:
    """完整保存逻辑消息；raw provider 请求不会写进 journal。"""

    return message.model_dump(mode="json")


def _message_from_payload(payload: Any) -> Optional[Message]:
    if not isinstance(payload, dict):
        return None
    try:
        return Message.model_validate(payload)
    except Exception:
        return None


def _checksum(items: Sequence[dict[str, Any]]) -> str:
    encoded = json.dumps(
        list(items),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _recovery_tool_results(
    items: Sequence[Message],
    checkpoints: dict[str, Message],
) -> tuple[list[Message], str]:
    """为崩溃前已持久化、但尚未配对的工具调用生成明确失败结果。"""

    pending: dict[str, str] = {}
    pending_turn_id = ""
    for message in items:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)
        if role == "assistant" and message.tool_calls:
            pending = {
                str(call.get("id") or ""): str(
                    ((call.get("function") or {}).get("name") or "")
                )
                for call in message.tool_calls
                if isinstance(call, dict) and call.get("id")
            }
            metadata = message.metadata if isinstance(message.metadata, dict) else {}
            pending_turn_id = str(metadata.get("turn_id") or "")
        elif role == "tool" and message.tool_call_id:
            pending.pop(str(message.tool_call_id), None)

    recovered: list[Message] = []
    for call_id, tool_name in pending.items():
        checkpoint = checkpoints.get(call_id)
        if checkpoint is not None:
            recovered.append(checkpoint.model_copy(deep=True))
            continue
        recovered.append(Message.create_tool_message(
            tool_call_id=call_id,
            tool_name=tool_name,
            tool_output=json.dumps(
                {
                    "error": "进程在工具结果持久化前退出，调用结果未知，禁止自动重放。",
                    "recovered": True,
                },
                ensure_ascii=False,
            ),
            is_error=True,
        ))
    return recovered, pending_turn_id


@dataclass
class JournalRecovery:
    history: ConversationHistory
    last_event_seq: int = 0
    warnings: list[str] = field(default_factory=list)
    migrated: bool = False


class HistoryJournal:
    """先写盘、后推进内存的唯一历史事务边界。"""

    def __init__(self, session_dir_provider: Callable[[], Path]) -> None:
        self._session_dir_provider = session_dir_provider
        self.last_event_seq = 0

    @property
    def path(self) -> Path:
        return self._session_dir_provider() / JOURNAL_FILE_NAME

    def recover(
        self,
        *,
        legacy_loader: Optional[Callable[[], Sequence[Message]]] = None,
    ) -> JournalRecovery:
        # 同一个 AgentSession 会在多个会话目录间切换，序号不能沿用上个目录。
        self.last_event_seq = 0
        path = self.path
        if not path.exists():
            legacy = list(legacy_loader() if legacy_loader is not None else [])
            if not legacy:
                return JournalRecovery(history=ConversationHistory())
            migrated = [freeze_history_message(message) for message in legacy]
            payloads = [_message_payload(message) for message in migrated]
            event = {
                "version": JOURNAL_VERSION,
                "type": "migration",
                "event_seq": 1,
                "generation": 1,
                "ts": _now_iso(),
                "items": payloads,
                "checksum": _checksum(payloads),
                "cache_reset_reason": "legacy_v3_migration",
            }
            self._append_event(event)
            self.last_event_seq = 1
            return JournalRecovery(
                history=ConversationHistory(migrated, generation=1),
                last_event_seq=1,
                migrated=True,
            )

        items: list[Message] = []
        generation = 0
        last_event_seq = 0
        warnings: list[str] = []
        tool_checkpoints: dict[str, Message] = {}
        for physical_line, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                warnings.append(f"history_journal_corrupt_line:{physical_line}")
                continue
            if not isinstance(event, dict) or event.get("version") != JOURNAL_VERSION:
                warnings.append(f"history_journal_invalid_event:{physical_line}")
                continue
            try:
                event_seq = int(event.get("event_seq") or 0)
            except (TypeError, ValueError):
                warnings.append(f"history_journal_invalid_seq:{physical_line}")
                continue
            # journal 只接受连续事件前缀。损坏事件本身不推进序号；同次恢复后新写入
            # 的事件会复用缺口序号，因此后续重启仍能越过保留下来的坏行继续恢复。
            if event_seq != last_event_seq + 1:
                warnings.append(f"history_journal_non_monotonic:{physical_line}")
                continue
            raw_items = event.get("items")
            if not isinstance(raw_items, list) or _checksum(raw_items) != event.get("checksum"):
                warnings.append(f"history_journal_checksum_failed:{physical_line}")
                continue
            restored = [message for value in raw_items if (message := _message_from_payload(value))]
            if len(restored) != len(raw_items):
                warnings.append(f"history_journal_message_invalid:{physical_line}")
                continue
            event_type = str(event.get("type") or "")
            try:
                event_generation = int(event.get("generation") or 0)
            except (TypeError, ValueError):
                warnings.append(f"history_journal_invalid_generation:{physical_line}")
                continue
            if event_type == "append":
                if event_generation != generation:
                    warnings.append(f"history_journal_generation_mismatch:{physical_line}")
                    continue
                items.extend(restored)
                # 工具结果正式进入 canonical history 后，对应 checkpoint 已完成使命。
                # call id 理论上应全局唯一，但恢复层不能依赖 provider 永不复用 id。
                for message in restored:
                    role = (
                        message.role.value
                        if hasattr(message.role, "value")
                        else str(message.role)
                    )
                    if role == "tool" and message.tool_call_id:
                        tool_checkpoints.pop(str(message.tool_call_id), None)
            elif event_type == "tool_checkpoint":
                if event_generation != generation:
                    warnings.append(f"history_journal_generation_mismatch:{physical_line}")
                    continue
                for message in restored:
                    if message.tool_call_id:
                        tool_checkpoints[str(message.tool_call_id)] = message
            elif event_type in {"replace", "migration"}:
                if event_type == "replace" and event_generation <= generation:
                    warnings.append(f"history_journal_stale_replace:{physical_line}")
                    continue
                items = restored
                generation = event_generation
                tool_checkpoints = {}
            else:
                warnings.append(f"history_journal_unknown_event:{physical_line}")
                continue
            last_event_seq = event_seq

        self.last_event_seq = last_event_seq
        history = ConversationHistory(items, generation=generation)
        recovered_tools, recovered_turn_id = _recovery_tool_results(
            items,
            tool_checkpoints,
        )
        if recovered_tools:
            self.append(
                history,
                recovered_tools,
                turn_id=recovered_turn_id,
                event_kind="recovery_tool_results",
            )
            warnings.append(
                f"history_journal_recovered_tool_results:{len(recovered_tools)}"
            )
        return JournalRecovery(
            history=history,
            last_event_seq=self.last_event_seq,
            warnings=warnings,
        )

    def append(
        self,
        history: ConversationHistory,
        messages: Sequence[Message],
        *,
        turn_id: str = "",
        event_kind: str = "append",
    ) -> list[Message]:
        prepared = history.prepare_batch(messages, turn_id=turn_id)
        if not prepared:
            return []
        payloads = [_message_payload(message) for message in prepared]
        event = {
            "version": JOURNAL_VERSION,
            "type": "append",
            "event_kind": str(event_kind or "append"),
            "event_seq": self.last_event_seq + 1,
            "generation": history.generation,
            "turn_id": str(turn_id or ""),
            "ts": _now_iso(),
            "items": payloads,
            "checksum": _checksum(payloads),
        }
        self._append_event(event)
        self.last_event_seq += 1
        history.append_prepared(prepared)
        return prepared

    def replace(
        self,
        history: ConversationHistory,
        messages: Sequence[Message],
        *,
        reason: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> list[Message]:
        prepared = [freeze_history_message(message) for message in messages]
        payloads = [_message_payload(message) for message in prepared]
        next_generation = history.generation + 1
        event = {
            "version": JOURNAL_VERSION,
            "type": "replace",
            "event_seq": self.last_event_seq + 1,
            "generation": next_generation,
            "from_generation": history.generation,
            "reason": str(reason or "compact"),
            "ts": _now_iso(),
            "items": payloads,
            "checksum": _checksum(payloads),
            "metadata": dict(metadata or {}),
        }
        self._append_event(event)
        self.last_event_seq += 1
        history.replace_prepared(prepared, generation=next_generation)
        return prepared

    def checkpoint_tool_result(
        self,
        history: ConversationHistory,
        message: Message,
        *,
        turn_id: str,
    ) -> None:
        """记录单个工具终态，但不把它提前暴露给模型 history。

        并行工具必须按 assistant 声明顺序批量进入模型上下文。检查点只用于进程
        中断恢复，正常批次完成后仍由 ``append`` 写入唯一的有序 tool messages。
        """

        prepared = freeze_history_message(message, turn_id=turn_id)
        payloads = [_message_payload(prepared)]
        event = {
            "version": JOURNAL_VERSION,
            "type": "tool_checkpoint",
            "event_kind": "tool_terminal",
            "event_seq": self.last_event_seq + 1,
            "generation": history.generation,
            "turn_id": str(turn_id or ""),
            "ts": _now_iso(),
            "items": payloads,
            "checksum": _checksum(payloads),
        }
        self._append_event(event)
        self.last_event_seq += 1

    def _append_event(self, event: dict[str, Any]) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = (
            json.dumps(event, ensure_ascii=False, default=str) + "\n"
        ).encode("utf-8")
        with path.open("ab+") as handle:
            # 进程可能在上一条 JSON 尚未写完时崩溃。若末尾没有换行，先隔开损坏
            # 尾段，避免下一条合法事件与其粘成一整条不可恢复的物理行。
            handle.seek(0, os.SEEK_END)
            if handle.tell() > 0:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    handle.seek(0, os.SEEK_END)
                    handle.write(b"\n")
            handle.seek(0, os.SEEK_END)
            handle.write(encoded)
            handle.flush()
            if os.getenv("CBAGENT_HISTORY_FSYNC", "").strip().lower() in {"1", "true", "yes", "on"}:
                os.fsync(handle.fileno())


__all__ = ["HistoryJournal", "JournalRecovery", "JOURNAL_VERSION"]
