"""hierarchical compact：无丢失 map/reduce 与预算上限。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent.compaction import (
    CompactionBudgetExceeded,
    CompactionError,
    SUMMARIZATION_PROMPT,
    run_local_compaction,
)
from core.message import Message


class _RecordingCompletions:
    def __init__(self, owner: "_RecordingLLM") -> None:
        self.owner = owner

    def create(self, **kwargs):
        self.owner.calls.append(kwargs)
        if self.owner.error is not None:
            raise self.owner.error
        summary = (
            self.owner.summaries.pop(0)
            if self.owner.summaries
            else f"partial-{len(self.owner.calls)}"
        )
        usage = None
        if self.owner.usage is not None:
            usage = SimpleNamespace(
                prompt_tokens=int(self.owner.usage.get("prompt_tokens", 0)),
                completion_tokens=int(self.owner.usage.get("completion_tokens", 0)),
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=summary, tool_calls=None))],
            usage=usage,
        )


class _RecordingLLM:
    def __init__(
        self,
        summaries: list[str] | None = None,
        *,
        usage: dict[str, int] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.model = "fake"
        self.output_token_param = "none"
        self.max_output_tokens = 1024
        self.summaries = list(summaries or [])
        self.calls: list[dict[str, Any]] = []
        self.usage = usage
        self.error = error
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=_RecordingCompletions(self)))


def _user(text: str) -> Message:
    return Message.create_user_message(text)


def _assistant(text: str) -> Message:
    return Message.create_assistant_message(text)


def _estimate_by_char_budget(messages: list[dict[str, Any]]) -> int:
    """用序列化字符数近似 token，便于在单测里人为控制是否超 hard limit。

    user content 可能是 list[part]；统一 json 风格 str() 再计长度。
    """
    total = 0
    for item in messages:
        content = item.get("content")
        if content is None:
            continue
        total += len(str(content))
    return total


# SUMMARIZATION_PROMPT 本身约 595 字符；单回合 + pad 约 720，两回合合计约 850。
# hard limit 取 800：单回合可装入，两回合以上触发 hierarchical。
_UNIT_HARD_LIMIT = 800
_PAD = "x" * 80


def test_single_pass_when_history_fits_hard_limit():
    llm = _RecordingLLM(summaries=["full handoff"])
    history = [_user("alpha-MARKER"), _assistant("done")]
    result = run_local_compaction(
        llm=llm,
        system_message=None,
        history=history,
        hard_limit_tokens=100_000,
        estimate_request_tokens=lambda messages: 100,
    )
    assert result.strategy == "single_pass"
    assert result.summary_requests == 1
    assert result.dropped_messages == 0
    assert result.source_message_count == 2
    assert result.covered_message_count == 2
    assert "full handoff" in result.summary
    assert len(llm.calls) == 1
    joined = str(llm.calls[0]["messages"])
    assert "alpha-MARKER" in joined
    assert "CONTEXT CHECKPOINT COMPACTION" in joined
    assert SUMMARIZATION_PROMPT.splitlines()[0] in joined


def test_hierarchical_covers_all_unique_markers():
    """超窗时 map/reduce，每个唯一 marker 至少进入一次摘要请求。"""

    llm = _RecordingLLM(summaries=["map-a", "map-b", "map-c", "final-reduce"])
    history = [
        _user(f"MARKER-A {_PAD}"),
        _assistant("reply-a"),
        _user(f"MARKER-B {_PAD}"),
        _assistant("reply-b"),
        _user(f"MARKER-C {_PAD}"),
        _assistant("reply-c"),
    ]

    result = run_local_compaction(
        llm=llm,
        system_message=None,
        history=history,
        hard_limit_tokens=_UNIT_HARD_LIMIT,
        estimate_request_tokens=_estimate_by_char_budget,
    )
    assert result.strategy == "hierarchical"
    assert result.dropped_messages == 0
    assert result.summary_requests >= 2
    assert result.covered_message_count == result.source_message_count == 6

    seen = {"MARKER-A": False, "MARKER-B": False, "MARKER-C": False}
    for call in llm.calls:
        blob = str(call["messages"])
        for marker in seen:
            if marker in blob:
                seen[marker] = True
    assert all(seen.values()), seen
    assert "final-reduce" in result.summary or result.summary


def test_budget_max_summary_requests_fails_without_installing():
    llm = _RecordingLLM(summaries=["x"] * 20)
    history = [
        _user(f"M1 {_PAD}"),
        _assistant("r1"),
        _user(f"M2 {_PAD}"),
        _assistant("r2"),
        _user(f"M3 {_PAD}"),
        _assistant("r3"),
    ]
    with pytest.raises(CompactionBudgetExceeded, match="请求次数"):
        run_local_compaction(
            llm=llm,
            system_message=None,
            history=history,
            hard_limit_tokens=_UNIT_HARD_LIMIT,
            estimate_request_tokens=_estimate_by_char_budget,
            budgets={
                "max_chunks": 8,
                "max_summary_requests": 1,
                "max_total_prompt_tokens": 10**9,
                "max_total_completion_tokens": 10**9,
            },
        )


def test_budget_max_chunks_fails():
    llm = _RecordingLLM(summaries=["x"] * 20)
    history = []
    for i in range(5):
        history.append(_user(f"Mk{i} {_PAD}"))
        history.append(_assistant(f"r{i}"))
    with pytest.raises(CompactionBudgetExceeded, match="chunk"):
        run_local_compaction(
            llm=llm,
            system_message=None,
            history=history,
            hard_limit_tokens=_UNIT_HARD_LIMIT,
            estimate_request_tokens=_estimate_by_char_budget,
            budgets={
                "max_chunks": 2,
                "max_summary_requests": 20,
                "max_total_prompt_tokens": 10**9,
                "max_total_completion_tokens": 10**9,
            },
        )


def test_budget_prompt_tokens_fails():
    llm = _RecordingLLM(summaries=["x"] * 20, usage={"prompt_tokens": 500, "completion_tokens": 10})
    history = [
        _user(f"P1 {_PAD}"),
        _assistant("r1"),
        _user(f"P2 {_PAD}"),
        _assistant("r2"),
    ]
    with pytest.raises(CompactionBudgetExceeded, match="prompt"):
        run_local_compaction(
            llm=llm,
            system_message=None,
            history=history,
            hard_limit_tokens=_UNIT_HARD_LIMIT,
            estimate_request_tokens=_estimate_by_char_budget,
            budgets={
                "max_chunks": 8,
                "max_summary_requests": 20,
                "max_total_prompt_tokens": 400,
                "max_total_completion_tokens": 10**9,
            },
        )


def test_budget_completion_tokens_fails():
    llm = _RecordingLLM(
        summaries=["x"] * 20,
        usage={"prompt_tokens": 10, "completion_tokens": 300},
    )
    history = [
        _user(f"C1 {_PAD}"),
        _assistant("r1"),
        _user(f"C2 {_PAD}"),
        _assistant("r2"),
    ]
    with pytest.raises(CompactionBudgetExceeded, match="completion"):
        run_local_compaction(
            llm=llm,
            system_message=None,
            history=history,
            hard_limit_tokens=_UNIT_HARD_LIMIT,
            estimate_request_tokens=_estimate_by_char_budget,
            budgets={
                "max_chunks": 8,
                "max_summary_requests": 20,
                "max_total_prompt_tokens": 10**9,
                "max_total_completion_tokens": 250,
            },
        )


def test_oversized_single_unit_fails_without_shrinking():
    llm = _RecordingLLM(summaries=["should-not-run"])
    history = [_user("HUGE " + ("H" * 500))]
    with pytest.raises(CompactionError, match="单条协议段超过"):
        run_local_compaction(
            llm=llm,
            system_message=None,
            history=history,
            hard_limit_tokens=50,
            estimate_request_tokens=_estimate_by_char_budget,
        )
    assert llm.calls == []


def test_tool_call_pairing_kept_in_each_chunk_request():
    """同一回合的 tool call/result 不得拆到不同摘要请求。"""

    llm = _RecordingLLM(summaries=["map1", "map2", "reduce"])
    history = [
        _user(f"turn1 {_PAD}"),
        Message.create_assistant_message(
            tool_calls=[{
                "id": "call-1",
                "type": "function",
                "function": {"name": "file_read", "arguments": "{}"},
            }]
        ),
        Message.create_tool_message("call-1", "file_read", "TOOL-BODY-1 " + ("x" * 20)),
        _assistant("after-tool-1"),
        _user(f"turn2 {_PAD}"),
        Message.create_assistant_message(
            tool_calls=[{
                "id": "call-2",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }]
        ),
        Message.create_tool_message("call-2", "bash", "TOOL-BODY-2 " + ("y" * 20)),
        _assistant("after-tool-2"),
    ]
    result = run_local_compaction(
        llm=llm,
        system_message=None,
        history=history,
        hard_limit_tokens=850,
        estimate_request_tokens=_estimate_by_char_budget,
    )
    assert result.strategy == "hierarchical"
    for call in llm.calls:
        blob = str(call["messages"])
        # 若某次请求出现 call-1，则同请求必须带上 tool result。
        if "call-1" in blob:
            assert "TOOL-BODY-1" in blob
        if "call-2" in blob:
            assert "TOOL-BODY-2" in blob
