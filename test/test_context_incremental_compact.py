"""Canonical history 的 world-state 与正式 compact 回归测试。"""

from __future__ import annotations

import base64
import copy
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from agent.compaction import (
    COMPACTION_SUMMARY_KIND,
    SUMMARY_PREFIX,
    SUMMARIZATION_PROMPT,
    CompactionError,
    dynamic_retained_token_target,
    estimate_message_tokens,
)
from agent.event_bus import EventBus
from agent.executor import ToolExecutor
from agent.media_store import MediaBlobStore
from agent.session import AgentSession
from agent.work_context import LocalSessionStore
from constant.llm.constant_llm import ConstantLLM
from context.world_state import DynamicSectionResult, WorldStateSnapshot
from core.message import Message, MessageRole


class FakeCompletions:
    def __init__(self, owner: "FakeLLM") -> None:
        self.owner = owner

    def create(self, **kwargs):
        self.owner.compact_calls.append(copy.deepcopy(kwargs))
        if self.owner.compact_error is not None:
            raise self.owner.compact_error
        summary = self.owner.summaries.pop(0) if self.owner.summaries else "handoff"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=summary, tool_calls=None))],
            usage=None,
        )


class FakeLLM:
    def __init__(self, answers=None, summaries=None) -> None:
        self.answers = list(answers or [])
        self.summaries = list(summaries or [])
        self.calls: list[dict[str, Any]] = []
        self.compact_calls: list[dict[str, Any]] = []
        self.compact_error: Exception | None = None
        self.is_Function_Calling = True
        self.model = "canonical-test-model"
        self.provider = "test"
        self.max_output_tokens = 4096
        self.output_token_param = "max_tokens"
        self.client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(self)))

    def think(self, messages, tools=None, **_kwargs):
        self.calls.append({"messages": copy.deepcopy(messages), "tools": copy.deepcopy(tools)})
        answer = self.answers.pop(0) if self.answers else "ok"
        return {"answer": answer, "tool_calls": []}

    def _apply_output_token_limit(self, request_kwargs):
        if self.output_token_param != "none":
            request_kwargs[self.output_token_param] = self.max_output_tokens


class FakeRegistry:
    def list_tools(self):
        return []

    def get_tools_description_openai_schema(self):
        return []


def _session(*, store=None, answers=None, summaries=None) -> AgentSession:
    bus = EventBus()
    llm = FakeLLM(answers=answers, summaries=summaries)
    return AgentSession(
        llm=llm,
        registry=FakeRegistry(),
        executor=ToolExecutor(lambda *_args, **_kwargs: "{}", bus),
        event_bus=bus,
        ctx_enabled=False,
        session_store=store,
    )


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str) -> Message:
    return Message.create_assistant_message(text)


def _context_text(messages: tuple[Message, ...]) -> str:
    return "\n".join(
        str(message.content or "")
        for message in messages
        if (message.metadata or {}).get("kind") == "context_update"
    )


def test_world_state_diff_and_turn_evidence_have_independent_lifecycles():
    async def first_sections(**_kwargs):
        return [
            DynamicSectionResult.present("environment", "ENV-A"),
            DynamicSectionResult.present("knowledge", "RAG-A", scope="turn_evidence"),
        ]

    session = _session()
    with patch("agent.session.get_dynamic_context_sections", new=first_sections):
        first = session._prepare_turn_input(
            user_content="first",
            runtime_guidance="",
            memory_query="first",
        )
    assert "ENV-A" in _context_text(first.messages)
    assert any("RAG-A" in str(message.content) for message in first.messages)
    session._append_history(first.messages, turn_id="turn-a")
    session._world_state_baseline = first.world_state

    async def second_sections(**_kwargs):
        return [
            DynamicSectionResult.present("environment", "ENV-B"),
            DynamicSectionResult.absent("knowledge", scope="turn_evidence"),
        ]

    with patch("agent.session.get_dynamic_context_sections", new=second_sections):
        second = session._prepare_turn_input(
            user_content="second",
            runtime_guidance="",
            memory_query="second",
        )
    update = _context_text(second.messages)
    assert "ENV-B" in update
    assert "knowledge" not in update
    assert "RAG-A" not in str(second.messages)
    assert "RAG-A" in str(session.history.provider_messages())


def test_dynamic_read_error_preserves_world_state_baseline():
    async def sections(**_kwargs):
        return [
            DynamicSectionResult.error_result("instructions", "temporary"),
            DynamicSectionResult.error_result("environment", "temporary"),
        ]

    session = _session()
    session._world_state_baseline = WorldStateSnapshot.from_sections([
        ("instructions", "KEEP-INSTRUCTIONS"),
        ("environment", "KEEP-ENV"),
    ])
    with patch("agent.session.get_dynamic_context_sections", new=sections):
        prepared = session._prepare_turn_input(
            user_content="continue",
            runtime_guidance="",
            memory_query="continue",
        )
    assert prepared.world_state == session._world_state_baseline
    assert not _context_text(prepared.messages)


def test_first_instructions_error_blocks_before_history_append():
    async def sections(**_kwargs):
        return [DynamicSectionResult.error_result("instructions", "unreadable")]

    session = _session()
    with patch("agent.session.get_dynamic_context_sections", new=sections):
        with pytest.raises(RuntimeError, match="关键 instructions 首次读取失败"):
            session._prepare_turn_input(
                user_content="continue",
                runtime_guidance="",
                memory_query="continue",
            )
    assert len(session.history) == 0


def test_restart_restores_world_state_from_canonical_journal():
    async def sections(**_kwargs):
        return [DynamicSectionResult.present("environment", "STABLE-ENV")]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "sessions"
        with patch("agent.session.get_dynamic_context_sections", new=sections):
            first = _session(store=LocalSessionStore(root), answers=["first"])
            first.chat("question")
            restarted = _session(store=LocalSessionStore(root))
            assert restarted._world_state_baseline == first._world_state_baseline
            prepared = restarted._prepare_turn_input(
                user_content="next",
                runtime_guidance="",
                memory_query="next",
            )
    assert "STABLE-ENV" not in _context_text(prepared.messages)


def test_restart_migrates_legacy_data_uri_once() -> None:
    """旧图片 history 在重启边界迁移一次，后续恢复不得重复创建 generation。"""

    with tempfile.TemporaryDirectory() as td:
        store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
        first = _session(store=store)
        data_uri = "data:image/png;base64," + base64.b64encode(b"legacy-image").decode("ascii")
        first._append_history([
            Message(
                role=MessageRole.USER,
                content=[{
                    "type": "image_url",
                    "image_url": {"url": data_uri},
                }],
            ),
            _assistant("done"),
        ], turn_id="legacy")

        restarted = _session(store=LocalSessionStore(store.root))
        first_generation = restarted.history.generation
        logical = str(restarted.history.logical_messages())
        provider = str(restarted._provider_request_messages())
        assert "image_ref" in logical
        assert data_uri not in logical
        assert data_uri in provider
        assert first_generation == 1

        restarted_again = _session(store=LocalSessionStore(store.root))
        assert restarted_again.history.generation == first_generation


def test_compact_summarizes_only_evicted_prefix_and_retains_latest_turn():
    session = _session(summaries=["PREFIX-HANDOFF"])
    session._append_history([_user("OLD-Q"), _assistant("OLD-A")], turn_id="old")
    session._append_history([_user("NEW-Q"), _assistant("NEW-A")], turn_id="new")
    latest_tokens = estimate_message_tokens(session.history[-2:])
    with patch("agent.session.dynamic_retained_token_target", return_value=latest_tokens):
        result = session.compact_context(reason="manual")

    assert result["no_op"] is False
    request = session.llm.compact_calls[0]["messages"]
    request_text = str(request)
    assert "OLD-Q" in request_text and "OLD-A" in request_text
    assert "NEW-Q" not in request_text and "NEW-A" not in request_text
    assert request[-1]["content"] == SUMMARIZATION_PROMPT
    assert [message.content for message in session.history[:2]] == ["NEW-Q", "NEW-A"]
    assert (session.history[-1].metadata or {}).get("kind") == COMPACTION_SUMMARY_KIND


def test_compact_summary_request_keeps_tool_protocol_complete():
    session = _session(summaries=["TOOL-HANDOFF"])
    calls = [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "file_read", "arguments": '{"path":"a.py"}'},
    }]
    session._append_history([
        _user("inspect"),
        Message.create_assistant_message(tool_calls=calls),
        Message.create_tool_message("call-1", "file_read", "FILE-MARKER"),
        _assistant("done"),
    ], turn_id="old")
    session._append_history([_user("latest"), _assistant("latest-answer")], turn_id="new")
    with patch("agent.session.dynamic_retained_token_target", return_value=100):
        session.compact_context(reason="auto")
    roles = [message["role"] for message in session.llm.compact_calls[0]["messages"]]
    assert roles[-5:] == ["user", "assistant", "tool", "assistant", "user"]


def test_manual_compact_resets_baseline_and_next_turn_reinjects_full_snapshot():
    async def sections(**_kwargs):
        return [DynamicSectionResult.present("environment", "ENV-NOW")]

    session = _session(summaries=["handoff"])
    session._append_history([_user("Q" * 20_000), _assistant("A" * 20_000)], turn_id="old")
    session._world_state_baseline = WorldStateSnapshot.from_sections([("environment", "ENV-NOW")])
    with (
        patch("agent.session.get_dynamic_context_sections", new=sections),
        patch("agent.session.dynamic_retained_token_target", return_value=100),
    ):
        session.compact_context(reason="manual")
        prepared = session._prepare_turn_input(
            user_content="next",
            runtime_guidance="",
            memory_query="next",
        )
    assert session._world_state_baseline.sections == {}
    assert "ENV-NOW" in _context_text(prepared.messages)


def test_mid_turn_compact_preserves_active_turn_and_places_summary_last():
    async def latest_sections(**_kwargs):
        return [DynamicSectionResult.present("environment", "ENV-NOW")]

    session = _session(summaries=["OLD-HANDOFF"])
    session._append_history([_user("OLD-Q"), _assistant("OLD-A")], turn_id="old")
    session._append_history([
        Message(
            role=MessageRole.USER,
            content="ENV-OLD",
            metadata={"kind": "context_update"},
        ),
        _user("ACTIVE-Q"),
        _assistant("ACTIVE-PROGRESS"),
    ], turn_id="active")
    session._world_state_baseline = WorldStateSnapshot.from_sections([("environment", "ENV-OLD")])
    with (
        patch("agent.session.dynamic_retained_token_target", return_value=0),
        patch("agent.session.get_dynamic_context_sections", new=latest_sections),
    ):
        result = session.compact_context(reason="mid_turn", active_turn_id="active")
    assert result["active_turn_tokens"] > 0
    contents = [str(message.content or "") for message in session.history]
    assert any("ENV-NOW" in content for content in contents)
    assert not any("ENV-OLD" in content for content in contents)
    assert "ACTIVE-Q" in contents
    assert "ACTIVE-PROGRESS" in contents
    assert SUMMARY_PREFIX in contents[-1]
    assert "ACTIVE-Q" not in str(session.llm.compact_calls[0]["messages"])


def test_mid_turn_compact_never_summarizes_only_active_turn():
    session = _session(summaries=["should-not-run"])
    session._append_history([_user("ACTIVE"), _assistant("PROGRESS")], turn_id="active")
    result = session.compact_context(reason="mid_turn", active_turn_id="active")
    assert result["no_op"] is True
    assert session.llm.compact_calls == []


def test_turn_aborted_is_not_treated_as_new_real_user_turn():
    """中止边界属于维护消息，不能在 compact 分段时开启一个伪用户回合。"""

    from agent.compaction import is_real_user_message

    marker = Message(
        role=MessageRole.USER,
        content="<turn_aborted />",
        metadata={"kind": "turn_aborted"},
    )
    assert is_real_user_message(marker) is False


def test_compaction_message_estimate_does_not_count_data_uri_as_text():
    """图片编码只作为稳定协议内容保留，不能按 base64 字符估算文本 token。"""

    image = Message(
        role=MessageRole.USER,
        content=[{
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64," + ("x" * 100_000),
            },
        }],
    )
    assert estimate_message_tokens([image]) < 1000


def test_compact_retained_tail_keeps_image_ref_restorable():
    """compact 只摘要旧前缀，最近完整回合的 ImageRef 必须原样保留。"""

    with tempfile.TemporaryDirectory() as td:
        session = _session(summaries=["OLD-HANDOFF"])
        session.media_store = MediaBlobStore(Path(td) / "media")
        ref = session.media_store.put_bytes(
            b"image bytes",
            mime_type="image/png",
            file_name="recent.png",
        )
        session._append_history([
            _user("OLD-Q " + "old " * 5000),
            _assistant("OLD-A " + "old " * 5000),
        ], turn_id="old")
        session._append_history([
            Message(
                role=MessageRole.USER,
                content=[
                    {"type": "text", "text": "RECENT-Q"},
                    {"type": "image_ref", "image_ref": ref.to_dict()},
                ],
            ),
            _assistant("RECENT-A"),
        ], turn_id="recent")

        with patch("agent.session.dynamic_retained_token_target", return_value=1000):
            result = session.compact_context(reason="auto")

        assert result["no_op"] is False
        logical = str(session.history.logical_messages())
        provider = str(session._provider_request_messages())
        assert "RECENT-Q" in logical
        assert "image_ref" in logical
        assert "data:image/png;base64," not in logical
        assert "data:image/png;base64," in provider


def test_compact_failure_keeps_history_and_generation_unchanged():
    session = _session()
    session._append_history([_user("OLD"), _assistant("OLD-A")], turn_id="old")
    session._append_history([_user("NEW"), _assistant("NEW-A")], turn_id="new")
    before = session.history.snapshot()
    generation = session.history.generation
    session.llm.compact_error = RuntimeError("network unavailable")
    with (
        patch("agent.session.dynamic_retained_token_target", return_value=10),
        pytest.raises(CompactionError),
    ):
        session.compact_context(reason="auto")
    assert session.history.snapshot() == before
    assert session.history.generation == generation


def test_compact_none_output_parameter_is_not_sent():
    session = _session(summaries=["handoff"])
    session.llm.output_token_param = "none"
    session._append_history([_user("OLD"), _assistant("OLD-A")], turn_id="old")
    session._append_history([_user("NEW"), _assistant("NEW-A")], turn_id="new")
    with patch("agent.session.dynamic_retained_token_target", return_value=10):
        session.compact_context(reason="auto")
    request = session.llm.compact_calls[0]
    assert "max_tokens" not in request
    assert "max_completion_tokens" not in request


def test_model_downshift_uses_target_window_for_replacement():
    original = ConstantLLM.llm_dict.get("small-target")
    ConstantLLM.llm_dict["small-target"] = {
        "is_tool": True,
        "is_reasoning": False,
        "max_tokens": 20_000,
        "max_output_tokens": 2_000,
    }
    try:
        session = _session(summaries=["downshift"])
        for index in range(4):
            session._append_history([
                _user(f"Q-{index}-" + "word " * 2500),
                _assistant(f"A-{index}-" + "word " * 2500),
            ], turn_id=f"turn-{index}")
        result = session.compact_context(
            reason="model_downshift",
            target_model="small-target",
        )
        assert result["no_op"] is False
        request_tokens = session._estimate_request_tokens(
            session._provider_request_messages(),
            [],
        )
        assert request_tokens <= ConstantLLM.context_limits("small-target")["soft_limit_tokens"]
    finally:
        if original is None:
            ConstantLLM.llm_dict.pop("small-target", None)
        else:
            ConstantLLM.llm_dict["small-target"] = original


@pytest.mark.parametrize(
    ("soft_limit", "expected"),
    [
        (8_000, 16 * 1024),
        (400_000, 40_000),
        (2_000_000, 128 * 1024),
    ],
)
def test_dynamic_retained_target_scales_with_window(soft_limit, expected):
    assert dynamic_retained_token_target(soft_limit) == expected
