"""把旧 transcript/compact/active 文件一次性迁移为 canonical history。

本模块只在目标会话尚无 ``history.jsonl`` 时调用。它不参与正常请求、compact
或恢复流程；迁移成功后，旧文件仅作为历史审计材料保留。
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from core.message import Message, MessageRole


@dataclass(frozen=True)
class _LegacyTurn:
    """旧 transcript 中一条可恢复记录。"""

    nonempty_ordinal: int
    turn_seq: int
    turn_id: str
    payload: dict[str, Any]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except Exception:
            continue
        if isinstance(value, dict):
            out.append(value)
    return out


def _message_from_payload(payload: Any, *, turn_id: str = "") -> Optional[Message]:
    """还原 v2/v3 保存的完整 provider 协议消息。"""

    if not isinstance(payload, dict):
        return None
    role = str(payload.get("role") or "")
    content = payload.get("content")
    if role == "user":
        if not isinstance(content, (str, list)) or content == "" or content is None:
            return None
        message = Message(role=MessageRole.USER, content=deepcopy(content))
    elif role == "system":
        if not isinstance(content, str) or not content:
            return None
        message = Message.create_system_message(content)
    elif role == "assistant":
        tool_calls = payload.get("tool_calls")
        text = content if isinstance(content, str) else None
        if text is None and not isinstance(tool_calls, list):
            return None
        message = Message.create_assistant_message(
            input_text=text,
            tool_calls=deepcopy(tool_calls) if isinstance(tool_calls, list) else None,
            reasoning_content=(
                str(payload.get("reasoning_content"))
                if payload.get("reasoning_content") is not None
                else None
            ),
        )
    elif role == "tool":
        call_id = str(payload.get("tool_call_id") or "")
        if not call_id:
            return None
        message = Message.create_tool_message(
            tool_call_id=call_id,
            tool_name=str(payload.get("tool_name") or payload.get("name") or ""),
            tool_output=str(content or ""),
            is_error=bool(payload.get("is_error")),
        )
    else:
        return None

    metadata: dict[str, Any] = {}
    for key in ("kind", "reason"):
        if payload.get(key):
            metadata[key] = str(payload.get(key))
    snapshot = payload.get("world_state_snapshot")
    if isinstance(snapshot, dict):
        metadata["world_state_snapshot"] = {
            str(name): str(value)
            for name, value in snapshot.items()
            if name and isinstance(value, str) and value.strip()
        }
    if payload.get("interrupted"):
        metadata["interrupted"] = True
    if turn_id:
        metadata["turn_id"] = turn_id
    if metadata:
        message.metadata = metadata
    return message


def _messages_from_payloads(
    payloads: Any,
    *,
    turn_id: str = "",
) -> list[Message]:
    if not isinstance(payloads, list):
        return []
    return [
        message
        for payload in payloads
        if (message := _message_from_payload(payload, turn_id=turn_id)) is not None
    ]


def _read_transcript(path: Path) -> list[_LegacyTurn]:
    """按非空物理行解释 v2 offset，并按 turn_seq 支持 v3 游标。"""

    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    records: list[_LegacyTurn] = []
    nonempty_ordinal = 0
    for line in lines:
        if not line.strip():
            continue
        nonempty_ordinal += 1
        try:
            value = json.loads(line)
        except Exception:
            continue
        if not isinstance(value, dict):
            continue
        try:
            turn_seq = int(value.get("turn_seq") or nonempty_ordinal)
        except (TypeError, ValueError):
            turn_seq = nonempty_ordinal
        records.append(_LegacyTurn(
            nonempty_ordinal=nonempty_ordinal,
            turn_seq=max(1, turn_seq),
            turn_id=str(value.get("turn_id") or ""),
            payload=value,
        ))

    # 同一个 turn_id 可能在“已写 transcript、未清 active”窗口重复出现。
    ordered = sorted(records, key=lambda item: (item.turn_seq, item.nonempty_ordinal))
    last_index = {
        item.turn_id: index
        for index, item in enumerate(ordered)
        if item.turn_id
    }
    return [
        item
        for index, item in enumerate(ordered)
        if not item.turn_id or last_index.get(item.turn_id) == index
    ]


def _active_turn_messages(events: Sequence[dict[str, Any]]) -> tuple[list[Message], str]:
    """恢复旧 active-turn 的完整工具配对，未知调用明确标记为失败。"""

    start_index = next(
        (
            index
            for index in range(len(events) - 1, -1, -1)
            if events[index].get("type") == "turn_started"
        ),
        -1,
    )
    if start_index < 0:
        return [], ""
    scoped = list(events[start_index:])
    started = scoped[0]
    turn_id = str(started.get("turn_id") or "")
    out: list[Message] = []
    context_message = _message_from_payload(
        started.get("context_update_payload"),
        turn_id=turn_id,
    )
    if context_message is not None:
        out.append(context_message)
    user_payload = started.get("user_payload")
    if not isinstance(user_payload, dict):
        user_payload = {"role": "user", "content": str(started.get("user_query") or "")}
    user_message = _message_from_payload(user_payload, turn_id=turn_id)
    if user_message is not None:
        out.append(user_message)

    terminal: dict[tuple[int, str], dict[str, Any]] = {}
    started_calls: set[tuple[int, str]] = set()
    for event in scoped[1:]:
        try:
            round_idx = int(event.get("round_idx") or 0)
        except (TypeError, ValueError):
            round_idx = 0
        call_id = str(event.get("tool_call_id") or "")
        if event.get("type") == "tool_started" and call_id:
            started_calls.add((round_idx, call_id))
        if event.get("type") in {"tool_terminal", "tool_completed"}:
            payload = event.get("tool_payload")
            if isinstance(payload, dict):
                resolved_id = str(payload.get("tool_call_id") or call_id)
                if resolved_id:
                    copied = deepcopy(payload)
                    copied["is_error"] = bool(copied.get("is_error") or event.get("is_error"))
                    terminal[(round_idx, resolved_id)] = copied

    for event in scoped[1:]:
        if event.get("type") != "assistant_tool_calls":
            continue
        payload = event.get("assistant_payload")
        calls = payload.get("tool_calls") if isinstance(payload, dict) else None
        if not isinstance(calls, list) or not calls:
            continue
        try:
            round_idx = int(event.get("round_idx") or 0)
        except (TypeError, ValueError):
            round_idx = 0
        assistant = _message_from_payload(payload, turn_id=turn_id)
        if assistant is None:
            continue
        out.append(assistant)
        for call in calls:
            if not isinstance(call, dict):
                continue
            call_id = str(call.get("id") or "")
            if not call_id:
                continue
            tool_payload = terminal.get((round_idx, call_id))
            tool_message = _message_from_payload(tool_payload, turn_id=turn_id)
            if tool_message is None:
                name = str(((call.get("function") or {}).get("name") or ""))
                status = (
                    "unknown"
                    if (round_idx, call_id) in started_calls
                    else "cancelled_before_start"
                )
                tool_message = Message.create_tool_message(
                    tool_call_id=call_id,
                    tool_name=name,
                    tool_output=json.dumps({
                        "status": status,
                        "reason": "legacy_process_interrupted",
                        "effect_state": "unknown" if status == "unknown" else "none",
                    }, ensure_ascii=False),
                    is_error=True,
                )
                tool_message.metadata = {"turn_id": turn_id} if turn_id else None
            out.append(tool_message)

    final_event = next(
        (
            event
            for event in reversed(scoped[1:])
            if event.get("type") == "assistant_final"
        ),
        None,
    )
    if isinstance(final_event, dict):
        final_message = _message_from_payload(
            final_event.get("assistant_payload"),
            turn_id=turn_id,
        )
        if final_message is not None:
            out.append(final_message)

    out.append(Message(
        role=MessageRole.USER,
        content=(
            "<turn-failed>\n"
            "旧会话在本轮完成提交前中断；未知工具不得自动重放。\n"
            "</turn-failed>"
        ),
        metadata={"kind": "turn_failed", "reason": "legacy_process_interrupted", "turn_id": turn_id},
    ))
    return out, turn_id


def _repair_legacy_tool_protocol(messages: Sequence[Message]) -> list[Message]:
    """迁移时修复旧窗口裁剪遗留的工具协议断层。

    这是旧数据的一次性兼容策略。v4 正常请求只做严格协议校验，绝不会调用本函数
    修改模型已经看过的 canonical history。无父工具结果直接丢弃；有父无结果则补
    明确的 unknown 终态，防止恢复后自动重放可能已经产生副作用的旧调用。
    """

    migrated: list[Message] = []
    pending: dict[str, str] = {}
    pending_turn_id = ""

    def _append_unknown_results() -> None:
        for call_id, tool_name in pending.items():
            unknown = Message.create_tool_message(
                tool_call_id=call_id,
                tool_name=tool_name,
                tool_output=json.dumps({
                    "status": "unknown",
                    "reason": "legacy_protocol_gap",
                    "effect_state": "unknown",
                }, ensure_ascii=False),
                is_error=True,
            )
            if pending_turn_id:
                unknown.metadata = {"turn_id": pending_turn_id}
            migrated.append(unknown)
        pending.clear()

    for message in messages:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)
        if pending:
            call_id = str(message.tool_call_id or "") if role == "tool" else ""
            if call_id in pending:
                migrated.append(message)
                pending.pop(call_id, None)
                continue
            _append_unknown_results()

        if role == "assistant":
            migrated.append(message)
            if message.tool_calls:
                pending = {
                    str(tool_call.get("id") or ""): str(
                        ((tool_call.get("function") or {}).get("name") or "")
                    )
                    for tool_call in message.tool_calls
                    if isinstance(tool_call, dict) and tool_call.get("id")
                }
                metadata = message.metadata if isinstance(message.metadata, dict) else {}
                pending_turn_id = str(metadata.get("turn_id") or "")
            continue
        if role == "tool":
            # 没有处于待配对块中的 tool 是旧裁剪留下的孤儿。
            continue
        migrated.append(message)
    if pending:
        _append_unknown_results()
    return migrated


def load_legacy_history(session_dir: Path) -> list[Message]:
    """读取一次 v2/v3 事实源，返回待写入 v4 migration 事件的消息。"""

    compact = _read_json(session_dir / "compact.json")
    records = _read_transcript(session_dir / "transcript.jsonl")
    version = compact.get("version")
    if version == 3:
        try:
            cursor = int(compact.get("transcript_cursor_seq") or 0)
        except (TypeError, ValueError):
            cursor = 0
        selected = [record for record in records if record.turn_seq > cursor]
    elif version == 2:
        try:
            offset = int(compact.get("transcript_offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        selected = [record for record in records if record.nonempty_ordinal > max(0, offset)]
    else:
        selected = records

    messages = _messages_from_payloads(compact.get("replacement_history"))
    committed_turn_ids = {record.turn_id for record in records if record.turn_id}
    for record in selected:
        messages.extend(_messages_from_payloads(
            record.payload.get("messages"),
            turn_id=record.turn_id,
        ))

    active_messages, active_turn_id = _active_turn_messages(
        _read_jsonl(session_dir / "active_turn.jsonl")
    )
    if active_turn_id and active_turn_id not in committed_turn_ids:
        messages.extend(active_messages)

    pending = _read_json(session_dir / "pending_user.json")
    pending_turn_id = str(pending.get("turn_id") or "")
    if (
        pending.get("user_query")
        and pending_turn_id not in committed_turn_ids
        and pending_turn_id != active_turn_id
    ):
        message = Message(
            role=MessageRole.USER,
            content=str(pending.get("user_query") or ""),
            metadata={"turn_id": pending_turn_id} if pending_turn_id else None,
        )
        messages.append(message)

    return _repair_legacy_tool_protocol(messages)


__all__ = ["load_legacy_history"]
