"""Codex 风格的本地上下文压缩核心。

压缩请求始终使用结构化消息历史，并在末尾追加一条交接摘要指令。这里不把历史
序列化为文本，也不提前构造旧式摘要输入切片；只有请求确实装不进当前模型窗口
时，才从最旧的协议完整段开始移除。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from context import count_tokens
from core.message import Message, MessageRole


SUMMARIZATION_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"""

COMPACTION_SUMMARY_KIND = "context_compaction"
CONTEXT_UPDATE_KIND = "context_update"


class CompactionError(RuntimeError):
    """表示压缩请求无法生成可安装的新历史。"""


@dataclass(frozen=True)
class CompactionModelResult:
    """一次结构化摘要请求的结果。"""

    summary: str
    attempts: int
    request_messages: list[dict[str, Any]]
    dropped_messages: int


@dataclass(frozen=True)
class RetainedHistory:
    """压缩后保留的最近原始回合。"""

    messages: list[Message]
    tokens: int
    oversized_latest_turn: bool = False


def message_kind(message: Message) -> str:
    """读取本地消息类型。"""

    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("kind") or "")


def message_role(message: Message) -> str:
    """统一返回消息角色字符串。"""

    role = message.role
    return role.value if hasattr(role, "value") else str(role)


def is_real_user_message(message: Message) -> bool:
    """判断消息是否为用户真实输入，而不是运行时状态或压缩摘要。"""

    return (
        message_role(message) == "user"
        and message_kind(message) not in {CONTEXT_UPDATE_KIND, COMPACTION_SUMMARY_KIND}
    )


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    """估算一组完整协议消息的 token 数。"""

    if not messages:
        return 0
    import json

    return count_tokens(
        json.dumps(
            [message.to_dict() for message in messages],
            ensure_ascii=False,
            default=str,
        )
    )


def dynamic_retained_token_target(soft_limit_tokens: int) -> int:
    """按 soft limit 的 10% 计算原始完整回合目标，限制在 16K 到 128K。"""

    soft_limit = max(1, int(soft_limit_tokens))
    return min(128 * 1024, max(16 * 1024, soft_limit // 10))


def _split_protocol_turns(messages: Sequence[Message]) -> list[list[Message]]:
    """把可保留历史拆成以真实用户输入开头的协议完整回合。"""

    cleaned = [
        message
        for message in messages
        if message_kind(message) not in {CONTEXT_UPDATE_KIND, COMPACTION_SUMMARY_KIND}
    ]
    user_positions = [
        index for index, message in enumerate(cleaned) if is_real_user_message(message)
    ]
    turns: list[list[Message]] = []
    for position, start in enumerate(user_positions):
        end = user_positions[position + 1] if position + 1 < len(user_positions) else len(cleaned)
        turns.append(cleaned[start:end])
    return turns


def _last_final_assistant(turn: Sequence[Message]) -> Optional[Message]:
    """返回回合中最后一条不带工具调用的助手正文。"""

    for message in reversed(turn):
        if message_role(message) != "assistant" or message.tool_calls:
            continue
        if isinstance(message.content, str) and message.content.strip():
            return message
    return None


def _clip_message_content(message: Message, token_budget: int) -> Message:
    """复制消息，并仅在超预算时裁剪可见正文。"""

    cloned = copy.deepcopy(message)
    text = cloned.content if isinstance(cloned.content, str) else str(cloned.content or "")
    if not text or count_tokens(text) <= token_budget:
        return cloned
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if count_tokens(text[:middle]) <= token_budget:
            low = middle
        else:
            high = middle - 1
    cloned.content = text[:low].rstrip() + "…"
    return cloned


def select_retained_history(
    messages: Sequence[Message],
    *,
    token_budget: int,
) -> RetainedHistory:
    """从最新回合向前选择原始历史，并保持工具协议块完整。"""

    budget = max(0, int(token_budget))
    if budget <= 0:
        return RetainedHistory(messages=[], tokens=0)
    turns = _split_protocol_turns(messages)
    if not turns:
        return RetainedHistory(messages=[], tokens=0)

    newest = turns[-1]
    newest_tokens = estimate_message_tokens(newest)
    if newest_tokens > budget:
        endpoints = [newest[0]]
        final_message = _last_final_assistant(newest)
        if final_message is not None and final_message is not newest[0]:
            endpoints.append(final_message)
        overhead = estimate_message_tokens([
            _clip_message_content(message, 0) for message in endpoints
        ])
        available = max(0, budget - overhead)
        allocations = [available] if len(endpoints) == 1 else [available // 2, available - available // 2]
        retained = [
            _clip_message_content(message, allocation)
            for message, allocation in zip(endpoints, allocations)
        ]
        while retained and estimate_message_tokens(retained) > budget:
            retained.pop()
        return RetainedHistory(
            messages=retained,
            tokens=estimate_message_tokens(retained),
            oversized_latest_turn=True,
        )

    selected_reversed: list[list[Message]] = []
    for turn in reversed(turns):
        candidate_reversed = [*selected_reversed, list(turn)]
        candidate = [message for item in reversed(candidate_reversed) for message in item]
        candidate_tokens = estimate_message_tokens(candidate)
        if selected_reversed and candidate_tokens > budget:
            break
        if not selected_reversed and candidate_tokens > budget:
            break
        selected_reversed.append(list(turn))
    selected_reversed.reverse()
    retained = [message for turn in selected_reversed for message in turn]
    return RetainedHistory(messages=retained, tokens=estimate_message_tokens(retained))


def make_summary_message(summary: str, *, reason: str) -> Message:
    """把模型摘要包装成 Codex 约定的 user 角色交接消息。"""

    content = str(summary or "").strip()
    if not content:
        raise CompactionError("压缩模型返回了空摘要")
    return Message(
        role=MessageRole.USER,
        content=f"{SUMMARY_PREFIX}\n{content}",
        metadata={"kind": COMPACTION_SUMMARY_KIND, "reason": str(reason or "")},
    )


def _history_units(messages: Sequence[Message]) -> list[list[Message]]:
    """构造按时间排序的可移除协议段，避免重试时拆散工具调用。"""

    units: list[list[Message]] = []
    current: list[Message] = []
    for message in messages:
        if is_real_user_message(message):
            if current:
                units.append(current)
            current = [message]
        elif current:
            current.append(message)
        else:
            units.append([message])
    if current:
        units.append(current)
    return units


def _flatten(units: Sequence[Sequence[Message]]) -> list[Message]:
    """把协议段恢复成消息序列。"""

    return [message for unit in units for message in unit]


def _shrink_largest_message_content(units: list[list[Message]]) -> bool:
    """把当前请求中最大的文本正文缩短一半，并保持协议外壳不变。"""

    largest: tuple[int, int, Optional[int], str] | None = None
    for unit_index, unit in enumerate(units):
        for message_index, message in enumerate(unit):
            content = message.content
            candidates: list[tuple[Optional[int], str]] = []
            if isinstance(content, str):
                candidates.append((None, content))
            elif isinstance(content, list):
                for part_index, part in enumerate(content):
                    if isinstance(part, dict) and part.get("type") == "text":
                        candidates.append((part_index, str(part.get("text") or "")))
            for part_index, text in candidates:
                if len(text) <= 1:
                    continue
                if largest is None or len(text) > len(largest[3]):
                    largest = (unit_index, message_index, part_index, text)
    if largest is None:
        return False
    unit_index, message_index, part_index, content = largest
    cloned = copy.deepcopy(units[unit_index][message_index])
    shortened = content[: max(1, len(content) // 2)].rstrip() + "…"
    if part_index is None:
        cloned.content = shortened
    else:
        cloned_parts = list(cloned.content) if isinstance(cloned.content, list) else []
        cloned_part = dict(cloned_parts[part_index])
        cloned_part["text"] = shortened
        cloned_parts[part_index] = cloned_part
        cloned.content = cloned_parts
    units[unit_index][message_index] = cloned
    return True


def _is_context_overflow_error(error: BaseException) -> bool:
    """保守识别 OpenAI-compatible provider 的上下文超限错误。"""

    text = str(error).lower()
    markers = (
        "context window",
        "maximum context",
        "context length",
        "too many tokens",
        "max_tokens",
    )
    return any(marker in text for marker in markers)


def run_local_compaction(
    *,
    llm: Any,
    system_message: Optional[dict[str, Any]],
    history: Sequence[Message],
    hard_limit_tokens: int,
    estimate_request_tokens: Callable[[list[dict[str, Any]]], int],
) -> CompactionModelResult:
    """执行 Codex 风格的结构化本地摘要，并在超窗时移除最旧协议段。"""

    client = getattr(llm, "client", None)
    model = getattr(llm, "model", None)
    if client is None or not model:
        raise CompactionError("当前模型没有可用的非流式客户端")

    units = _history_units(history)
    dropped_messages = 0
    attempts = 0
    while True:
        active_history = _flatten(units)
        request_messages: list[dict[str, Any]] = []
        if system_message:
            request_messages.append(dict(system_message))
        request_messages.extend(message.to_dict() for message in active_history)
        request_messages.append({"role": "user", "content": SUMMARIZATION_PROMPT})

        if estimate_request_tokens(request_messages) > max(1, int(hard_limit_tokens)):
            if len(units) > 1:
                removed = units.pop(0)
                dropped_messages += len(removed)
                continue
            # 单个用户回合或工具结果就可能超过窗口。此时保留角色、tool_call_id
            # 和调用配对，只逐步缩短最大的正文，直到请求能够交给摘要模型。
            if not _shrink_largest_message_content(units):
                raise CompactionError("压缩请求即使缩短超大消息后仍超过模型窗口")
            continue

        attempts += 1
        request_kwargs: dict[str, Any] = {
            "model": model,
            "stream": False,
            "messages": request_messages,
        }
        apply_limit = getattr(llm, "_apply_output_token_limit", None)
        if callable(apply_limit):
            apply_limit(request_kwargs)
        else:
            output_param = str(getattr(llm, "output_token_param", "max_tokens") or "max_tokens")
            if output_param != "none":
                request_kwargs[output_param] = int(getattr(llm, "max_output_tokens", 16 * 1024))
        try:
            response = client.chat.completions.create(**request_kwargs)
        except Exception as error:
            if _is_context_overflow_error(error):
                if len(units) > 1:
                    removed = units.pop(0)
                    dropped_messages += len(removed)
                    continue
                if _shrink_largest_message_content(units):
                    continue
            raise CompactionError(f"本地压缩请求失败: {error}") from error

        try:
            summary = str(response.choices[0].message.content or "").strip()
        except Exception as error:
            raise CompactionError("压缩响应缺少 assistant summary") from error
        if not summary:
            raise CompactionError("压缩模型返回了空摘要")
        return CompactionModelResult(
            summary=summary,
            attempts=attempts,
            request_messages=request_messages,
            dropped_messages=dropped_messages,
        )


__all__ = [
    "COMPACTION_SUMMARY_KIND",
    "CompactionError",
    "CompactionModelResult",
    "RetainedHistory",
    "SUMMARY_PREFIX",
    "SUMMARIZATION_PROMPT",
    "dynamic_retained_token_target",
    "estimate_message_tokens",
    "make_summary_message",
    "message_kind",
    "run_local_compaction",
    "select_retained_history",
]
