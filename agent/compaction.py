"""Codex 风格的本地上下文压缩核心。

压缩请求始终使用结构化消息历史，并在末尾追加一条交接摘要指令。这里不把历史
序列化为文本，也不提前构造旧式摘要输入切片。

当单次摘要请求装不进 hard limit 时，按 user 回合协议段做 hierarchical
map/reduce：每条 source 消息至少进入一次摘要请求；命中预算上限则整体失败，
禁止未摘要丢弃最旧消息。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from agent.llm_errors import (
    LLMContextOverflowError,
    LLMRequestError,
    classify_llm_exception,
)
from agent.compaction_view import build_compaction_view
from agent.media_store import estimate_visual_tokens_in_payload
from agent.multimodal_input import sanitize_multimodal_payload
from context import count_tokens
from core.message import Message, MessageRole


SUMMARIZATION_PROMPT = """You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary for another LLM that will resume the task.

This is a special summarization request, not a normal agent turn. Do not call tools and do not imitate or emit any tool-call protocol. Return plain Markdown prose only.

Include:
- Current progress and key decisions made
- Important context, constraints, or user preferences
- What remains to be done (clear next steps)
- Any critical data, examples, or references needed to continue

Be concise, structured, and focused on helping the next LLM seamlessly continue the work."""

SUMMARY_PREFIX = """Another language model started to solve this problem and produced a summary of its thinking process. You also have access to the state of the tools that were used by that language model. Use this to build on the work that has already been done and avoid duplicating work. Here is the summary produced by the other language model, use the information in this summary to assist with your own analysis:"""

COMPACTION_SUMMARY_KIND = "context_compaction"
CONTEXT_UPDATE_KIND = "context_update"
NON_TURN_USER_KINDS = {
    CONTEXT_UPDATE_KIND,
    COMPACTION_SUMMARY_KIND,
    "context_evidence",
    "plan_state",
    "tool_image_bridge",
    "turn_failed",
    "turn_aborted",
}


class CompactionError(RuntimeError):
    """表示压缩请求无法生成可安装的新历史。"""


class CompactionProviderError(CompactionError):
    """摘要 provider 请求失败，并保留结构化错误供上层决策。"""

    def __init__(self, message: str, llm_error: LLMRequestError) -> None:
        super().__init__(message)
        self.llm_error = llm_error


_TOOL_CALL_PROTOCOL_MARKERS = (
    "<｜｜dsml｜｜tool_calls>",
    "<｜｜dsml｜｜invoke",
    "<|dsml|>tool_calls",
    "<tool_call",
    "<function_calls",
)


def _validated_summary_from_response(response: Any) -> str:
    """提取纯文本摘要，并拒绝模型误生成的工具调用协议。

    compact 使用普通非流式请求且不会提供工具 schema。部分模型仍会受历史中的
    Agent 指令影响，把下一步操作以文本工具调用协议输出。此类内容不是交接摘要，
    一旦安装会把完整历史替换成一条无意义命令，因此必须在持久化前拦截。
    """

    try:
        message = response.choices[0].message
    except Exception as error:
        raise CompactionError("压缩响应缺少 assistant summary") from error

    if getattr(message, "tool_calls", None):
        raise CompactionError("压缩模型错误返回了工具调用，拒绝替换会话历史")

    summary = str(getattr(message, "content", None) or "").strip()
    if not summary:
        raise CompactionError("压缩模型返回了空摘要")

    # 只规范大小写，不删除内容；全角 DSML 标记也能按原样识别。
    normalized = summary.lower()
    if any(marker in normalized for marker in _TOOL_CALL_PROTOCOL_MARKERS):
        raise CompactionError("压缩模型返回了文本化工具调用，拒绝替换会话历史")
    return summary


class CompactionBudgetExceeded(CompactionError):
    """hierarchical compact 命中 chunk/请求/token 硬上限时抛出。

    上层必须保持 history / compact 快照 / world state 完全不变。
    """


@dataclass(frozen=True)
class CompactionModelResult:
    """一次结构化摘要（single-pass 或 hierarchical map/reduce）的结果。"""

    summary: str
    attempts: int
    strategy: str = "single_pass"
    summary_requests: int = 0
    summary_prompt_tokens: int = 0
    summary_output_tokens: int = 0
    source_message_count: int = 0
    covered_message_count: int = 0


@dataclass(frozen=True)
class CompactionPartition:
    """一次 compact 的旧前缀、保留尾部和活动回合分区。"""

    summarized_prefix: list[Message]
    retained_tail: list[Message]
    active_turn: list[Message]
    retained_tokens: int
    active_tokens: int
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
        and message_kind(message) not in NON_TURN_USER_KINDS
    )


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    """估算完整协议消息的文本和视觉 token，不把 base64 当正文。"""

    if not messages:
        return 0
    import json

    logical_payload = [message.to_dict() for message in messages]
    text_tokens = count_tokens(
        json.dumps(
            sanitize_multimodal_payload(logical_payload),
            ensure_ascii=False,
            default=str,
        )
    )
    return text_tokens + estimate_visual_tokens_in_payload(logical_payload)


def dynamic_retained_token_target(soft_limit_tokens: int) -> int:
    """按 soft limit 的 10% 计算原始完整回合目标，限制在 16K 到 128K。"""

    soft_limit = max(1, int(soft_limit_tokens))
    return min(128 * 1024, max(16 * 1024, soft_limit // 10))


def _message_turn_id(message: Message) -> str:
    """读取 canonical journal 为消息分配的用户回合标识。"""

    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("turn_id") or "")


def _split_complete_units(messages: Sequence[Message]) -> list[list[Message]]:
    """把旧历史拆成不可再细分的完整回合单元。

    v4 消息优先按 ``turn_id`` 分组。一次性迁移的旧消息可能没有该字段，此时退回
    真实 user 边界；assistant.tool_calls 与后续 tool 结果始终留在同一单元内。
    """

    units: list[list[Message]] = []
    current: list[Message] = []
    current_turn_id = ""
    for message in messages:
        turn_id = _message_turn_id(message)
        if turn_id:
            if current and current_turn_id and turn_id != current_turn_id:
                units.append(current)
                current = []
            elif current and not current_turn_id and is_real_user_message(message):
                units.append(current)
                current = []
            current.append(message)
            current_turn_id = turn_id
            continue

        if is_real_user_message(message) and current:
            units.append(current)
            current = [message]
            current_turn_id = ""
        else:
            current.append(message)
    if current:
        units.append(current)
    return units


def partition_history_for_compaction(
    messages: Sequence[Message],
    *,
    retained_token_budget: int,
    active_turn_id: str = "",
) -> CompactionPartition:
    """选择要摘要的旧前缀，并原样保留最近完整回合与活动回合。

    活动回合从首次出现目标 ``turn_id`` 的位置一直保留到历史末尾。这样连续发生
    多次 mid-turn compact 时，前一次插入且影响了后续采样的摘要也不会从当前任务
    现场中消失。若最近旧回合本身超过预算，则不切半条协议链，只保留摘要。
    """

    source = list(messages)
    active_start: Optional[int] = None
    if active_turn_id:
        active_start = next(
            (
                index
                for index, message in enumerate(source)
                if _message_turn_id(message) == active_turn_id
            ),
            None,
        )
    old_history = source[:active_start] if active_start is not None else source
    active_turn = (
        [
            message
            for message in source[active_start:]
            # 最新完整 world state 会在 replacement 前重新插入；活动回合内的旧
            # context_update 若继续保留，会在它之后覆盖刚刷新的现场。
            if message_kind(message) != CONTEXT_UPDATE_KIND
        ]
        if active_start is not None
        else []
    )

    units = _split_complete_units(old_history)
    budget = max(0, int(retained_token_budget))
    selected_count = 0
    retained_tokens = 0
    oversized_latest_turn = False
    for unit in reversed(units):
        candidate_units = units[len(units) - selected_count - 1:]
        candidate = [item for group in candidate_units for item in group]
        candidate_tokens = estimate_message_tokens(candidate)
        if candidate_tokens > budget:
            if selected_count == 0:
                oversized_latest_turn = True
            break
        selected_count += 1
        retained_tokens = candidate_tokens

    split_at = len(units) - selected_count
    summarized_prefix = [item for unit in units[:split_at] for item in unit]
    retained_tail = [item for unit in units[split_at:] for item in unit]
    return CompactionPartition(
        summarized_prefix=summarized_prefix,
        retained_tail=retained_tail,
        active_turn=active_turn,
        retained_tokens=retained_tokens,
        active_tokens=estimate_message_tokens(active_turn),
        oversized_latest_turn=oversized_latest_turn,
    )


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


def _default_compaction_budgets(
    hard_limit_tokens: int,
    *,
    max_output_tokens: int,
) -> dict[str, int]:
    """hierarchical compact 的默认硬上限（可被调用方覆盖）。"""

    hard = max(1, int(hard_limit_tokens))
    out = max(1, int(max_output_tokens))
    return {
        "max_chunks": 8,
        "max_summary_requests": 12,
        "max_total_prompt_tokens": 4 * hard,
        "max_total_completion_tokens": min(64 * 1024, 4 * out),
    }


def _build_summary_request_messages(
    *,
    system_message: Optional[dict[str, Any]],
    history_messages: Sequence[Message],
) -> list[dict[str, Any]]:
    """构造一次摘要请求：可选 system + 结构化历史 + 交接指令。"""

    request_messages: list[dict[str, Any]] = []
    if system_message:
        request_messages.append(dict(system_message))
    # 摘要模型只看图片清单。retained tail 和 active turn 不走该视图，安装后仍
    # 保存原始 ImageRef，并在下一次普通 provider 请求边界重新展开。
    request_messages.extend(build_compaction_view(history_messages))
    request_messages.append({"role": "user", "content": SUMMARIZATION_PROMPT})
    return request_messages


def _response_usage_tokens(response: Any) -> tuple[int, int]:
    """从 provider usage 提取 prompt/completion tokens；缺失时返回 (0, 0)。"""

    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion = int(getattr(usage, "completion_tokens", 0) or 0)
    return max(0, prompt), max(0, completion)


def _pack_units_into_chunks(
    units: Sequence[Sequence[Message]],
    *,
    system_message: Optional[dict[str, Any]],
    hard_limit_tokens: int,
    estimate_request_tokens: Callable[[list[dict[str, Any]]], int],
) -> list[list[list[Message]]]:
    """把协议段尽量装入不超过 hard limit 的 chunk，保持 tool 配对完整。"""

    limit = max(1, int(hard_limit_tokens))
    chunks: list[list[list[Message]]] = []
    current: list[list[Message]] = []

    def _fits(candidate_units: Sequence[Sequence[Message]]) -> bool:
        request = _build_summary_request_messages(
            system_message=system_message,
            history_messages=_flatten(candidate_units),
        )
        return estimate_request_tokens(request) <= limit

    for unit in units:
        unit_list = [list(unit)]
        if not current:
            if not _fits(unit_list):
                raise CompactionError(
                    "单条协议段超过摘要模型窗口，无法无丢失压缩；"
                    "请创建新会话或换更大上下文窗口的模型"
                )
            current = unit_list
            continue
        candidate = [*current, list(unit)]
        if _fits(candidate):
            current = candidate
        else:
            chunks.append(current)
            if not _fits(unit_list):
                raise CompactionError(
                    "单条协议段超过摘要模型窗口，无法无丢失压缩；"
                    "请创建新会话或换更大上下文窗口的模型"
                )
            current = unit_list
    if current:
        chunks.append(current)
    return chunks


class _CompactionBudgetTracker:
    """跟踪 hierarchical compact 的请求次数与累计 token 预算。"""

    def __init__(self, budgets: dict[str, int]) -> None:
        self.max_chunks = int(budgets["max_chunks"])
        self.max_summary_requests = int(budgets["max_summary_requests"])
        self.max_total_prompt_tokens = int(budgets["max_total_prompt_tokens"])
        self.max_total_completion_tokens = int(budgets["max_total_completion_tokens"])
        self.summary_requests = 0
        self.summary_prompt_tokens = 0
        self.summary_output_tokens = 0
        self.chunks_used = 0

    def ensure_room_for_request(
        self,
        estimated_prompt_tokens: int,
        estimated_completion_tokens: int,
    ) -> None:
        if self.summary_requests + 1 > self.max_summary_requests:
            raise CompactionBudgetExceeded(
                f"compact 请求次数超过上限 ({self.max_summary_requests})"
            )
        if self.summary_prompt_tokens + max(0, estimated_prompt_tokens) > self.max_total_prompt_tokens:
            raise CompactionBudgetExceeded(
                f"compact 累计 prompt tokens 超过上限 ({self.max_total_prompt_tokens})"
            )
        if (
            self.summary_output_tokens + max(0, estimated_completion_tokens)
            > self.max_total_completion_tokens
        ):
            raise CompactionBudgetExceeded(
                f"compact 累计 completion tokens 超过上限 ({self.max_total_completion_tokens})"
            )

    def note_chunks(self, chunk_count: int) -> None:
        self.chunks_used += max(0, int(chunk_count))
        if self.chunks_used > self.max_chunks:
            raise CompactionBudgetExceeded(
                f"compact chunk 数超过上限 ({self.max_chunks})"
            )

    def settle(
        self,
        *,
        estimated_prompt_tokens: int,
        usage_prompt: int,
        usage_completion: int,
        estimated_completion_tokens: int,
    ) -> None:
        prompt = usage_prompt if usage_prompt > 0 else max(0, estimated_prompt_tokens)
        completion = (
            usage_completion if usage_completion > 0 else max(0, estimated_completion_tokens)
        )
        self.summary_requests += 1
        self.summary_prompt_tokens += prompt
        self.summary_output_tokens += completion
        if self.summary_prompt_tokens > self.max_total_prompt_tokens:
            raise CompactionBudgetExceeded(
                f"compact 累计 prompt tokens 超过上限 ({self.max_total_prompt_tokens})"
            )
        if self.summary_output_tokens > self.max_total_completion_tokens:
            raise CompactionBudgetExceeded(
                f"compact 累计 completion tokens 超过上限 ({self.max_total_completion_tokens})"
            )


def run_local_compaction(
    *,
    llm: Any,
    system_message: Optional[dict[str, Any]],
    history: Sequence[Message],
    hard_limit_tokens: int,
    estimate_request_tokens: Callable[[list[dict[str, Any]]], int],
    budgets: Optional[dict[str, int]] = None,
) -> CompactionModelResult:
    """执行 Codex 风格结构化摘要；超窗时无丢失分段 map/reduce。

    正常路径优先 single-pass。只有摘要输入装不进 hard limit 时才启用 hierarchical：
    按 user 回合协议段切 chunk → 局部 handoff → reduce 到唯一最终摘要。

    不变量：source history 中每条消息至少进入一次真正发出的摘要请求；
    命中预算上限时抛 CompactionBudgetExceeded，不返回局部 handoff。
    """

    client = getattr(llm, "client", None)
    model = getattr(llm, "model", None)
    if client is None or not model:
        raise CompactionError("当前模型没有可用的非流式客户端")

    source_messages = list(history)
    source_count = len(source_messages)
    if source_count == 0:
        raise CompactionError("没有可压缩的历史消息")

    hard_limit = max(1, int(hard_limit_tokens))
    max_output = int(getattr(llm, "max_output_tokens", 16 * 1024) or 16 * 1024)
    tracker = _CompactionBudgetTracker(
        {**_default_compaction_budgets(hard_limit, max_output_tokens=max_output), **(budgets or {})}
    )
    covered_ids: set[int] = set()

    def _mark_covered(messages: Sequence[Message]) -> None:
        for message in messages:
            covered_ids.add(id(message))

    def _call_summary(request_messages: list[dict[str, Any]]) -> str:
        estimated_prompt = max(0, int(estimate_request_tokens(request_messages)))
        tracker.ensure_room_for_request(estimated_prompt, max_output)
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
                request_kwargs[output_param] = max_output
        try:
            response = client.chat.completions.create(**request_kwargs)
        except Exception as error:
            typed_error = classify_llm_exception(
                error,
                provider=str(getattr(llm, "provider", "") or ""),
                model_key=str(getattr(llm, "current_model_key", "") or ""),
                model_id=str(model or ""),
            )
            if isinstance(typed_error, LLMContextOverflowError):
                raise CompactionError(
                    "摘要请求超过模型窗口，且无法在不丢消息的前提下继续压缩"
                ) from typed_error
            raise CompactionProviderError(
                f"本地压缩请求失败: {typed_error}",
                typed_error,
            ) from typed_error
        usage_prompt, usage_completion = _response_usage_tokens(response)
        # provider 不返回 usage 时，completion 用输出上限的保守估算结算。
        tracker.settle(
            estimated_prompt_tokens=estimated_prompt,
            usage_prompt=usage_prompt,
            usage_completion=usage_completion,
            estimated_completion_tokens=max_output if usage_completion <= 0 else usage_completion,
        )
        return _validated_summary_from_response(response)

    def _result(summary: str, *, strategy: str) -> CompactionModelResult:
        covered = min(source_count, len(covered_ids))
        if covered < source_count:
            raise CompactionError(
                f"compact 覆盖不完整: covered={covered} source={source_count}"
            )
        return CompactionModelResult(
            summary=summary,
            attempts=tracker.summary_requests,
            strategy=strategy,
            summary_requests=tracker.summary_requests,
            summary_prompt_tokens=tracker.summary_prompt_tokens,
            summary_output_tokens=tracker.summary_output_tokens,
            source_message_count=source_count,
            covered_message_count=covered,
        )

    units = _history_units(source_messages)
    single_request = _build_summary_request_messages(
        system_message=system_message,
        history_messages=source_messages,
    )
    # 仅本地估算的初始 single-pass 探测不计 summary_requests。
    if estimate_request_tokens(single_request) <= hard_limit:
        _mark_covered(source_messages)
        summary = _call_summary(single_request)
        return _result(summary, strategy="single_pass")

    # ---- hierarchical map/reduce ----
    # 工作队列元素：要么是原始协议段 list[Message]，要么是已生成的局部 summary Message。
    work_units: list[list[Message]] = [list(unit) for unit in units]
    while True:
        if not work_units:
            raise CompactionError("hierarchical compact 工作队列为空")
        probe = _build_summary_request_messages(
            system_message=system_message,
            history_messages=_flatten(work_units),
        )
        if estimate_request_tokens(probe) <= hard_limit:
            for unit in work_units:
                _mark_covered(unit)
            summary = _call_summary(probe)
            return _result(summary, strategy="hierarchical")

        chunks = _pack_units_into_chunks(
            work_units,
            system_message=system_message,
            hard_limit_tokens=hard_limit,
            estimate_request_tokens=estimate_request_tokens,
        )
        if len(chunks) <= 1:
            # 装不进但仍是一整块：单段本身超窗（已在 pack 中校验）或估算抖动。
            raise CompactionError(
                "历史无法拆成可摘要的多个 chunk，且单次请求超过 hard limit"
            )
        tracker.note_chunks(len(chunks))
        next_units: list[list[Message]] = []
        for chunk in chunks:
            chunk_messages = _flatten(chunk)
            _mark_covered(chunk_messages)
            request = _build_summary_request_messages(
                system_message=system_message,
                history_messages=chunk_messages,
            )
            partial = _call_summary(request)
            # 中间层 handoff 不用最终 SUMMARY_PREFIX（过长会导致 reduce 再次超窗）。
            # 最终返回的是纯文本 summary，由 session 侧 make_summary_message 包装。
            next_units.append([
                Message(
                    role=MessageRole.USER,
                    content=f"[hierarchical partial handoff]\n{partial}",
                    metadata={
                        "kind": COMPACTION_SUMMARY_KIND,
                        "reason": "hierarchical_map",
                    },
                )
            ])
        work_units = next_units


__all__ = [
    "COMPACTION_SUMMARY_KIND",
    "CompactionBudgetExceeded",
    "CompactionError",
    "CompactionProviderError",
    "CompactionModelResult",
    "SUMMARY_PREFIX",
    "SUMMARIZATION_PROMPT",
    "dynamic_retained_token_target",
    "estimate_message_tokens",
    "make_summary_message",
    "message_kind",
    "run_local_compaction",
    "partition_history_for_compaction",
]
