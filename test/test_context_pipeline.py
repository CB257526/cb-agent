"""上下文提示、预算窗口与 replacement history 选择测试。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from constant.llm.constant_llm import ConstantLLM
from agent.compaction import estimate_message_tokens, select_retained_history
from context.budget.window import get_context_window_for_model
from context.prompts.builder import (
    get_dynamic_context_prompt,
    get_dynamic_context_sections,
    get_static_system_prompt,
    get_system_prompt,
)
from core.message import Message, MessageRole


@pytest.fixture(autouse=True)
def _isolate_context_window_env(monkeypatch):
    monkeypatch.delenv(ConstantLLM.ENV_MAX_TOKENS, raising=False)
    monkeypatch.delenv("CB_AGENT_MAX_CONTEXT_TOKENS", raising=False)


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str = "", *, tool_calls=None) -> Message:
    return Message.create_assistant_message(text or None, tool_calls=tool_calls)


def _tool(call_id: str, text: str) -> Message:
    return Message.create_tool_message(call_id, "file_read", text)


def _text(message: Message) -> str:
    return str(message.content or "")


def test_static_prompt_has_no_runtime_context_or_tool_inventory():
    static = get_static_system_prompt(enabled_tools=frozenset({"bash", "file_read"}))
    text = "\n\n".join(static)

    assert "You are cb-agent" in text
    assert "# Current date" not in text
    assert "# Environment" not in text
    assert "Available tools:" not in text


def test_dynamic_context_returns_named_sections_in_stable_order(tmp_path: Path):
    sections = asyncio.run(get_dynamic_context_sections(
        enabled_tools=frozenset({"bash", "file_read"}),
        model="cache-test-model",
        cwd=tmp_path,
        language="Chinese",
    ))

    assert [name for name, _ in sections] == [
        "session_guidance",
        "current_date",
        "environment",
        "language",
    ]
    text = "\n\n".join(content for _, content in sections)
    assert "Available tools: bash, file_read." in text
    assert "# Current date" in text
    assert "# Environment" in text
    assert f"Working directory: {tmp_path.resolve()}" in text
    assert "# Language" in text
    # 时间块只保留日期和时区，不应包含每秒变化的时分秒。
    date_text = dict(sections)["current_date"]
    assert "Current local time" not in date_text


def test_dynamic_prompt_compat_returns_only_section_text(tmp_path: Path):
    dynamic = asyncio.run(get_dynamic_context_prompt(
        enabled_tools=frozenset({"bash"}),
        model="cache-test-model",
        cwd=tmp_path,
    ))
    assert all(isinstance(item, str) for item in dynamic)
    assert "# Current date" in "\n\n".join(dynamic)


def test_get_system_prompt_compat_returns_static_then_dynamic(tmp_path: Path):
    parts = asyncio.run(get_system_prompt(
        enabled_tools=frozenset({"bash"}),
        model="cache-test-model",
        cwd=tmp_path,
    ))
    text = "\n\n".join(parts)

    assert "You are cb-agent" in text
    assert text.index("You are cb-agent") < text.index("# Session guidance")
    assert "# Current date" in text


def test_window_default():
    assert get_context_window_for_model("totally-unknown-model") == 200_000


def test_window_1m_suffix():
    assert get_context_window_for_model("foo[1m]") == 1_000_000
    assert get_context_window_for_model("foo[1M]") == 1_000_000


def test_window_env_override(monkeypatch):
    monkeypatch.setenv("CB_AGENT_MAX_CONTEXT_TOKENS", "12345")
    assert get_context_window_for_model("anything") == 12345


def test_window_beta_header_falls_through_when_no_registry_match():
    assert get_context_window_for_model(
        "x-y-z-not-in-registry",
        betas=["context-1m-2025-08-07"],
    ) == 1_000_000


def test_window_reads_constant_llm_registry():
    assert get_context_window_for_model("deepseek-v4-flash") == 1_000_000


def test_compaction_selects_complete_turns_from_newest_backwards():
    call = {
        "id": "call_new",
        "type": "function",
        "function": {"name": "file_read", "arguments": '{"path":"new.py"}'},
    }
    oldest = [_user("old-user"), _assistant("old-answer")]
    middle = [_user("middle-user"), _assistant("middle-answer")]
    newest = [
        _user("new-user"),
        _assistant(tool_calls=[call]),
        _tool("call_new", "new-tool-result"),
        _assistant("new-final"),
    ]
    budget = estimate_message_tokens(middle + newest)

    selection = select_retained_history(
        oldest + middle + newest,
        token_budget=budget,
    )

    retained_text = "\n".join(_text(message) for message in selection.messages)
    assert "old-user" not in retained_text
    assert "middle-user" in retained_text
    assert "new-tool-result" in retained_text
    assert selection.tokens <= budget
    assert [message.tool_call_id for message in selection.messages if message.tool_call_id] == [
        "call_new"
    ]


def test_oversized_latest_turn_keeps_user_and_final_answer_only():
    call = {
        "id": "call_big",
        "type": "function",
        "function": {"name": "file_read", "arguments": "{}"},
    }
    newest = [
        _user("latest-user"),
        _assistant(tool_calls=[call]),
        _tool("call_big", "T" * 20_000),
        _assistant("latest-final"),
    ]

    selection = select_retained_history(newest, token_budget=300)

    assert selection.oversized_latest_turn
    assert [_text(message) for message in selection.messages] == [
        "latest-user",
        "latest-final",
    ]
