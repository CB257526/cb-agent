"""Compact replacement history 的回合选择逻辑。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Sequence

from core.message import Message

from ..budget.tokens import count_tokens
from .boundary import is_compact_boundary


CONTEXT_UPDATE_KIND = "context_update"
DEFAULT_RETAINED_MESSAGE_TOKENS = 64_000


@dataclass(frozen=True)
class CompactionSelection:
    """一次 compact 的摘要输入与原始尾部选择结果。"""

    summary_source: list[Message]
    retained_messages: list[Message]
    retained_tokens: int
    oversized_latest_turn: bool = False


def _role(message: Message) -> str:
    role = message.role
    return role.value if hasattr(role, "value") else str(role)


def _kind(message: Message) -> str:
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("kind") or "")


def _is_real_user_message(message: Message) -> bool:
    return (
        _role(message) == "user"
        and _kind(message) not in {CONTEXT_UPDATE_KIND, "compact_boundary"}
    )


def estimate_messages_tokens(messages: Sequence[Message]) -> int:
    """估算完整协议消息 token，包含 tool_calls 参数和 tool result。"""
    if not messages:
        return 0
    payload = [message.to_dict() for message in messages]
    return count_tokens(json.dumps(payload, ensure_ascii=False, default=str))


def _split_complete_turns(messages: Sequence[Message]) -> tuple[list[Message], list[list[Message]]]:
    """把 history 拆成首个真实 user 前缀与完整用户回合。"""
    cleaned = [message for message in messages if _kind(message) != CONTEXT_UPDATE_KIND]
    user_positions = [
        index for index, message in enumerate(cleaned) if _is_real_user_message(message)
    ]
    if not user_positions:
        return cleaned, []

    prefix = cleaned[: user_positions[0]]
    turns: list[list[Message]] = []
    for position, start in enumerate(user_positions):
        end = user_positions[position + 1] if position + 1 < len(user_positions) else len(cleaned)
        turns.append(cleaned[start:end])
    return prefix, turns


def _last_final_assistant_index(turn: Sequence[Message]) -> int:
    """查找回合中最后一条有正文且不声明工具调用的 assistant。"""
    for index in range(len(turn) - 1, -1, -1):
        message = turn[index]
        if _role(message) != "assistant" or message.tool_calls:
            continue
        if isinstance(message.content, str) and message.content.strip():
            return index
    return -1


def _content_text(message: Message) -> str:
    """提取可按 token 裁剪的消息正文。"""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(part for part in parts if part)
    return "" if content is None else str(content)


def _clip_text_tokens(text: str, max_tokens: int) -> str:
    """按 token 上限保留文本开头。"""
    if not text or max_tokens <= 0:
        return ""
    if count_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…"


def _copy_with_content_budget(message: Message, token_budget: int) -> Message:
    """复制消息并把正文裁到指定 token 预算。"""
    cloned = copy.deepcopy(message)
    cloned.content = _clip_text_tokens(_content_text(message), token_budget)
    return cloned


def _fit_oversized_endpoints(
    user_message: Message,
    final_message: Message | None,
    budget: int,
) -> tuple[list[Message], bool]:
    """把超大回合的用户输入与最终回答限制在硬预算内。

    正常情况下两条消息会原样返回。只有它们自身合计也超过预算时才按正文 token
    比例裁剪；完整原文随后仍会进入 summary_source，不会在压缩输入中消失。
    """
    endpoints = [user_message] + ([final_message] if final_message is not None else [])
    if estimate_messages_tokens(endpoints) <= budget:
        return endpoints, False

    empty_endpoints = [_copy_with_content_budget(message, 0) for message in endpoints]
    overhead = estimate_messages_tokens(empty_endpoints)
    if overhead >= budget:
        # 极端小预算连协议外壳都装不下时，只能全部进入摘要，避免突破硬上限。
        return [], True

    available = max(1, budget - overhead)
    source_tokens = [max(1, count_tokens(_content_text(message))) for message in endpoints]
    if len(endpoints) == 1:
        allocations = [available]
    else:
        # 先给用户输入与最终回答各保留一半；短的一侧用不完时把余额让给另一侧。
        first = available // 2
        second = available - first
        allocations = [first, second]
        for index in range(2):
            unused = max(0, allocations[index] - source_tokens[index])
            allocations[index] -= unused
            allocations[1 - index] += unused

    fitted = [
        _copy_with_content_budget(message, allocation)
        for message, allocation in zip(endpoints, allocations)
    ]
    # JSON 包装和转义可能带来少量估算差异，按比例继续收紧直到满足硬预算。
    if estimate_messages_tokens(fitted) > budget:
        low, high = 0, 1000
        best = empty_endpoints
        while low <= high:
            scale = (low + high) // 2
            candidate = [
                _copy_with_content_budget(message, max(0, allocation * scale // 1000))
                for message, allocation in zip(endpoints, allocations)
            ]
            if estimate_messages_tokens(candidate) <= budget:
                best = candidate
                low = scale + 1
            else:
                high = scale - 1
        fitted = best
    return fitted, True


def select_compaction_history(
    messages: Sequence[Message],
    *,
    retained_token_budget: int = DEFAULT_RETAINED_MESSAGE_TOKENS,
) -> CompactionSelection:
    """从最新回合向前选择 replacement history 原始尾部。

    选择只在完整用户回合边界发生。若最新回合单独超出预算，则优先原样保留该轮
    用户输入与最终回答，中间工具链进入摘要；若首尾自身也超限，再按硬预算裁剪，
    同时把完整原回合放入摘要输入，避免截断 tool-call/result 协议块。
    """
    budget = max(1, int(retained_token_budget))
    prefix, turns = _split_complete_turns(messages)
    if not turns:
        return CompactionSelection(
            summary_source=prefix,
            retained_messages=[],
            retained_tokens=0,
        )

    newest_turn = turns[-1]
    newest_tokens = estimate_messages_tokens(newest_turn)
    if newest_tokens > budget:
        final_index = _last_final_assistant_index(newest_turn)
        final_message = newest_turn[final_index] if final_index > 0 else None
        retained, endpoints_trimmed = _fit_oversized_endpoints(
            newest_turn[0],
            final_message,
            budget,
        )
        summary_middle = list(newest_turn[1:])
        if final_index > 0:
            summary_middle = list(newest_turn[1:final_index]) + list(newest_turn[final_index + 1:])
        if endpoints_trimmed:
            # 首尾消息被裁剪时，摘要输入必须拿到完整原回合以弥补被裁掉的正文。
            summary_middle = list(newest_turn)
        summary_source = [*prefix]
        for turn in turns[:-1]:
            summary_source.extend(turn)
        summary_source.extend(summary_middle)
        return CompactionSelection(
            summary_source=summary_source,
            retained_messages=retained,
            retained_tokens=estimate_messages_tokens(retained),
            oversized_latest_turn=True,
        )

    retained_turns: list[list[Message]] = []
    retained_tokens = 0
    first_retained_turn = len(turns)
    for index in range(len(turns) - 1, -1, -1):
        turn = turns[index]
        turn_tokens = estimate_messages_tokens(turn)
        if retained_turns and retained_tokens + turn_tokens > budget:
            break
        if not retained_turns and turn_tokens > budget:
            break
        retained_turns.append(turn)
        retained_tokens += turn_tokens
        first_retained_turn = index
    retained_turns.reverse()

    summary_source = list(prefix)
    for turn in turns[:first_retained_turn]:
        summary_source.extend(turn)
    retained_messages = [message for turn in retained_turns for message in turn]
    return CompactionSelection(
        summary_source=summary_source,
        retained_messages=retained_messages,
        retained_tokens=retained_tokens,
    )


def has_meaningful_summary_source(messages: Sequence[Message]) -> bool:
    """判断摘要输入是否包含旧摘要之外的可压缩消息。"""
    return any(not is_compact_boundary(message) for message in messages)


__all__ = [
    "CompactionSelection",
    "DEFAULT_RETAINED_MESSAGE_TOKENS",
    "estimate_messages_tokens",
    "has_meaningful_summary_source",
    "select_compaction_history",
]
