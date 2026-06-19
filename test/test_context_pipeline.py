"""Context prompt, budget, and compaction pipeline tests."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from constant.llm.constant_llm import ConstantLLM
from context import clear_system_prompt_sections
from context.budget.window import get_context_window_for_model
from context.compact.auto_compact import maybe_auto_compact
from context.compact.boundary import (
    COMPACT_BOUNDARY_KIND,
    find_last_compact_boundary,
    is_compact_boundary,
    make_compact_boundary_message,
    messages_after_last_boundary,
)
from context.compact.summarizer import RuleBasedSummarizer
from context.prompts.builder import (
    get_dynamic_context_prompt,
    get_static_system_prompt,
    get_system_prompt,
)
from core.message import Message


@pytest.fixture(autouse=True)
def _isolate_context_window_env(monkeypatch):
    monkeypatch.delenv(ConstantLLM.ENV_MAX_TOKENS, raising=False)
    monkeypatch.delenv("CB_AGENT_MAX_CONTEXT_TOKENS", raising=False)


# --- Chat prompt cache layout ---


def test_static_prompt_has_no_runtime_context_or_tool_inventory():
    static = get_static_system_prompt(enabled_tools=frozenset({"bash", "file_read"}))
    text = "\n\n".join(static)

    assert "You are cb-agent" in text
    assert "# Current time" not in text
    assert "# Environment" not in text
    assert "Available tools:" not in text


def test_dynamic_context_contains_runtime_context_and_tools(tmp_path: Path):
    clear_system_prompt_sections()

    dynamic = asyncio.run(get_dynamic_context_prompt(
        enabled_tools=frozenset({"bash", "file_read"}),
        model="cache-test-model",
        cwd=tmp_path,
        language="Chinese",
    ))
    text = "\n\n".join(dynamic)

    assert "# Session guidance" in text
    assert "Available tools: bash, file_read." in text
    assert "# Current time" in text
    assert "# Environment" in text
    assert f"Working directory: {tmp_path.resolve()}" in text
    assert "# Language" in text


def test_get_system_prompt_compat_returns_static_then_dynamic(tmp_path: Path):
    clear_system_prompt_sections()

    parts = asyncio.run(get_system_prompt(
        enabled_tools=frozenset({"bash"}),
        model="cache-test-model",
        cwd=tmp_path,
    ))
    text = "\n\n".join(parts)

    assert "You are cb-agent" in text
    assert text.index("You are cb-agent") < text.index("# Session guidance")
    assert "# Current time" in text


# --- Budget window ---


def test_window_default():
    assert get_context_window_for_model("totally-unknown-model") == 200_000


def test_window_1m_suffix():
    assert get_context_window_for_model("foo[1m]") == 1_000_000
    assert get_context_window_for_model("foo[1M]") == 1_000_000


def test_window_env_override(monkeypatch):
    monkeypatch.setenv("CB_AGENT_MAX_CONTEXT_TOKENS", "12345")
    assert get_context_window_for_model("anything") == 12345


def test_window_beta_header_falls_through_when_no_registry_match():
    out = get_context_window_for_model(
        "x-y-z-not-in-registry",
        betas=["context-1m-2025-08-07"],
    )
    assert out == 1_000_000


def test_window_reads_constant_llm_registry():
    assert get_context_window_for_model("deepseek-v4-flash") == 1_000_000


# --- CompactBoundary marker ---


def test_make_and_find_compact_boundary():
    msgs = [
        Message.create_user_message("q1"),
        Message.create_assistant_message("a1"),
    ]
    b = make_compact_boundary_message(
        summary="prev work summary",
        tokens_before=5000,
        reason="auto_compact:test",
    )
    assert is_compact_boundary(b)
    assert b.metadata["kind"] == COMPACT_BOUNDARY_KIND
    msgs.insert(0, b)
    idx = find_last_compact_boundary(msgs)
    assert idx == 0


def test_messages_after_last_boundary():
    msgs = [
        Message.create_user_message("old"),
        make_compact_boundary_message(summary="s1"),
        Message.create_user_message("recent"),
    ]
    after = messages_after_last_boundary(msgs)
    assert len(after) == 2
    assert is_compact_boundary(after[0])


def test_messages_after_last_boundary_no_boundary():
    msgs = [Message.create_user_message("u")]
    after = messages_after_last_boundary(msgs)
    assert len(after) == 1


# --- auto_compact ---


def test_auto_compact_under_threshold_returns_false():
    msgs = [
        Message.create_user_message("hi"),
        Message.create_assistant_message("hello"),
    ]
    result = asyncio.run(maybe_auto_compact(
        msgs,
        model="deepseek-v4-flash",
        summarizer=None,
        threshold_pct=0.85,
    ))
    assert not result.triggered
    assert result.tokens_after == result.tokens_before


def test_auto_compact_force_triggers_with_rule_summarizer():
    msgs = [
        Message.create_user_message("query " + "x " * 20),
        Message.create_assistant_message("answer " + "y " * 20),
        Message.create_user_message("more " + "z " * 20),
        Message.create_assistant_message("done " + "w " * 20),
    ]
    summarizer = RuleBasedSummarizer(max_messages=4, max_chars_per_msg=50)
    result = asyncio.run(maybe_auto_compact(
        msgs,
        model="some-model",
        summarizer=summarizer,
        force=True,
        keep_recent_messages=1,
    ))
    assert result.triggered
    assert result.summary is not None
    assert any(is_compact_boundary(m) for m in msgs)


def test_auto_compact_session_memory_path_zero_api():
    class FakeState:
        def state_text(self):
            return "task=X; files_seen=a.py,b.py; pending=write tests"

    msgs = [Message.create_user_message("hello"), Message.create_assistant_message("hi")] * 5
    counter = {"calls": 0}

    class CountingSummarizer:
        async def summarize(self, m, focus=None):
            counter["calls"] += 1
            return "should not be used"

    result = asyncio.run(maybe_auto_compact(
        msgs,
        model="any",
        summarizer=CountingSummarizer(),
        session_state=FakeState(),
        force=True,
        keep_recent_messages=2,
    ))
    assert result.triggered
    assert counter["calls"] == 0
    assert "task=X" in (result.summary or "")
