"""Cache split / boundary / auto_compact 单元测试。

覆盖:
- split_sys_prompt_prefix Path A (含 BOUNDARY) 与 Path B (无 BOUNDARY)
- build_system_prompt_blocks 与 OpenAI/Anthropic adapter 输出
- get_context_window_for_model 五级优先级
- compact_boundary 标记的 round-trip
- maybe_auto_compact 阈值未达不触发 / 达到触发 / SessionState 路径优先
- cached_microcompact 触发阈值与 placeholder 替换
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from context.budget.window import get_context_window_for_model
from context.cache.blocks import build_system_prompt_blocks
from context.cache.provider_adapter import (
    AnthropicAdapter,
    OpenAICompatibleAdapter,
)
from context.cache.scope import CacheScope
from context.cache.split import split_sys_prompt_prefix
from context.compact.auto_compact import maybe_auto_compact
from context.compact.boundary import (
    COMPACT_BOUNDARY_KIND,
    find_last_compact_boundary,
    is_compact_boundary,
    make_compact_boundary_message,
    messages_after_last_boundary,
)
from context.compact.cached_microcompact import (
    CachedMCState,
    KEEP_RECENT,
    PLACEHOLDER_TEXT,
    TRIGGER_THRESHOLD,
    maybe_microcompact_tool_results,
)
from context.compact.summarizer import RuleBasedSummarizer
from context.prompts.boundary import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from core.message import Message, MessageRole


# --- Cache split ---

def test_split_pathB_no_boundary():
    parts = ["intro", "system", "memory"]
    out = split_sys_prompt_prefix(parts, use_global_cache_scope=False)
    assert len(out) == 1
    assert out[0].scope == CacheScope.ORG
    assert "intro" in out[0].text
    assert "memory" in out[0].text


def test_split_pathA_with_boundary():
    parts = ["intro", "system", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "memory", "env"]
    out = split_sys_prompt_prefix(parts, use_global_cache_scope=True)
    assert len(out) == 2
    assert out[0].scope == CacheScope.GLOBAL
    assert "intro" in out[0].text
    assert "system" in out[0].text
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in out[0].text
    assert out[1].scope == CacheScope.NONE
    assert "memory" in out[1].text
    assert "env" in out[1].text


def test_split_boundary_dropped_when_pathB():
    parts = ["intro", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "memory"]
    out = split_sys_prompt_prefix(parts, use_global_cache_scope=False)
    assert len(out) == 1
    # boundary marker 应被滤除
    assert SYSTEM_PROMPT_DYNAMIC_BOUNDARY not in out[0].text


def test_split_empty_input():
    assert split_sys_prompt_prefix([], use_global_cache_scope=True) == []
    assert split_sys_prompt_prefix(["", "  "], use_global_cache_scope=False) == []


def test_build_system_prompt_blocks_invokes_split():
    blocks = build_system_prompt_blocks(
        ["a", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "b"],
        use_global_cache_scope=True,
    )
    assert len(blocks) == 2
    assert blocks[0].scope == CacheScope.GLOBAL
    assert blocks[1].scope == CacheScope.NONE


def test_openai_adapter_joins_to_string():
    adapter = OpenAICompatibleAdapter()
    blocks = build_system_prompt_blocks(["a", "b"], use_global_cache_scope=False)
    out = adapter.emit_system(blocks)
    assert isinstance(out, str)
    assert "a" in out and "b" in out


def test_anthropic_adapter_returns_dict_list():
    adapter = AnthropicAdapter()
    blocks = build_system_prompt_blocks(
        ["s", SYSTEM_PROMPT_DYNAMIC_BOUNDARY, "d"],
        use_global_cache_scope=True,
    )
    out = adapter.emit_system(blocks)
    assert isinstance(out, list)
    assert all(d["type"] == "text" for d in out)
    # GLOBAL/ORG 段应有 cache_control
    assert any("cache_control" in d for d in out)


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
    # deepseek-v4-flash 在 ConstantLLM 注册表中 max_tokens=1_000_000
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
    # 包含 boundary 自身和它之后的消息
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
        model="deepseek-v4-flash",  # 1M window
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
    # 边界 marker 已插入
    assert any(is_compact_boundary(m) for m in msgs)


def test_auto_compact_session_memory_path_zero_api():
    """SessionState 摘要路径优先于 LLM,不调 summarizer。"""
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
    assert counter["calls"] == 0  # 没调到 LLM summarizer
    assert "task=X" in (result.summary or "")


# --- cached_microcompact ---

def _make_tool_msg(call_id: str, content: str = "result") -> Message:
    return Message(
        role=MessageRole.TOOL,
        tool_call_id=call_id,
        tool_name="bash",
        content=content,
    )


def test_cached_microcompact_no_op_below_threshold():
    state = CachedMCState()
    msgs = [_make_tool_msg(f"id-{i}") for i in range(TRIGGER_THRESHOLD)]
    elided = maybe_microcompact_tool_results(msgs, state)
    assert elided == 0
    assert all(m.content == "result" for m in msgs)


def test_cached_microcompact_elides_old_tool_results():
    state = CachedMCState()
    msgs = [_make_tool_msg(f"id-{i}") for i in range(TRIGGER_THRESHOLD + 5)]
    elided = maybe_microcompact_tool_results(msgs, state)
    assert elided > 0
    # 最近 KEEP_RECENT 条 content 不变
    recent_ok = sum(1 for m in msgs if m.content == "result")
    assert recent_ok == KEEP_RECENT
    # 其余 content 是 placeholder
    placeholder_count = sum(1 for m in msgs if m.content == PLACEHOLDER_TEXT)
    assert placeholder_count == elided


def test_cached_microcompact_idempotent():
    """二次调用不应重复 elide 已 elide 的消息。"""
    state = CachedMCState()
    msgs = [_make_tool_msg(f"id-{i}") for i in range(TRIGGER_THRESHOLD + 3)]
    first = maybe_microcompact_tool_results(msgs, state)
    second = maybe_microcompact_tool_results(msgs, state)
    assert first > 0
    assert second == 0
