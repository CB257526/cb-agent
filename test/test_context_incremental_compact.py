"""增量上下文指纹与连续 compact 回归测试。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.event_bus import EventBus
from agent.executor import ToolExecutor
from agent.session import AgentSession
from agent.work_context import LocalSessionStore, _message_to_persist_payload
from context.compact import make_compact_boundary_message
from core.message import Message, MessageRole


class FakeLLM:
    """记录请求并按顺序返回固定回答。"""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.calls: list[dict[str, Any]] = []
        self.is_Function_Calling = True
        self.model = "fake"

    def think(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        answer = self.answers.pop(0) if self.answers else "ok"
        return {"answer": answer, "tool_calls": []}


class FakeRegistry:
    """提供稳定的最小工具注册表。"""

    def list_tools(self):
        return []

    def get_tools_description_openai_schema(self):
        return []


def _session(*, store: LocalSessionStore | None = None, answers=None) -> AgentSession:
    bus = EventBus()
    registry = FakeRegistry()
    return AgentSession(
        llm=FakeLLM(answers),
        registry=registry,
        executor=ToolExecutor(lambda *_args, **_kwargs: "{}", bus),
        event_bus=bus,
        ctx_enabled=False,
        session_store=store,
    )


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str) -> Message:
    return Message.create_assistant_message(text)


def _context_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message for message in messages
        if message.get("role") == "user"
        and "<context-update>" in str(message.get("content") or "")
    ]


def test_section_diff_only_emits_changed_and_removed_sections():
    current = [
        ("environment", "ENV-A"),
        ("knowledge", "KNOWLEDGE-A"),
    ]

    async def dynamic_sections(**_kwargs):
        return list(current)

    session = _session()
    with patch("agent.session.get_dynamic_context_sections", new=dynamic_sections):
        first = session._build_chat_messages(
            user_content="first",
            system_instructions="",
        )
        first_update = _context_messages(first)[0]["content"]
        assert "ENV-A" in first_update
        assert "KNOWLEDGE-A" in first_update

        # 模拟回合成功提交后推进累计指纹基线。
        session._context_fingerprints = dict(session._pending_context_fingerprints)
        second = session._build_chat_messages(
            user_content="second",
            system_instructions="",
        )
        assert _context_messages(second) == []

        current[:] = [
            ("environment", "ENV-A"),
            ("knowledge", "KNOWLEDGE-B"),
        ]
        third = session._build_chat_messages(
            user_content="third",
            system_instructions="",
        )
        third_update = _context_messages(third)[0]["content"]
        assert "KNOWLEDGE-B" in third_update
        assert "ENV-A" not in third_update

        session._context_fingerprints = dict(session._pending_context_fingerprints)
        current[:] = [("environment", "ENV-A")]
        fourth = session._build_chat_messages(
            user_content="fourth",
            system_instructions="",
        )
        fourth_update = _context_messages(fourth)[0]["content"]
        assert '<context-section name="knowledge" state="removed" />' in fourth_update
        assert "ENV-A" not in fourth_update


def test_restart_recovers_fingerprint_and_avoids_full_reinjection():
    async def dynamic_sections(**_kwargs):
        return [("environment", "STABLE-ENV")]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / ".cbagent" / "sessions"
        store = LocalSessionStore(root)
        with (
            patch("agent.session.get_dynamic_context_sections", new=dynamic_sections),
            patch.object(AgentSession, "_build_system_instructions", return_value=""),
        ):
            first = _session(store=store, answers=["first-answer"])
            first.chat("first-question")
            expected = dict(first._context_fingerprints)
            assert expected

            restarted = _session(store=LocalSessionStore(root))
            assert restarted._context_fingerprints == expected
            request = restarted._build_chat_messages(
                user_content="second-question",
                system_instructions="",
            )
            # 请求中仍包含上一轮已提交的 context update；关键是本轮没有再追加一条。
            assert len(_context_messages(request)) == 1
            assert restarted._pending_context_update_text == ""


def test_compact_clears_fingerprint_and_next_turn_reinjects_all_sections():
    async def dynamic_sections(**_kwargs):
        return [("environment", "STABLE-ENV")]

    session = _session(answers=["answer-1", "answer-2"])
    with (
        patch("agent.session.get_dynamic_context_sections", new=dynamic_sections),
        patch.object(AgentSession, "_build_system_instructions", return_value=""),
    ):
        session.chat("question-1")
        session.chat("question-2")
        assert session._context_fingerprints

        with patch("agent.session.COMPACT_RETAINED_MESSAGE_TOKENS", 20):
            result = session.compact_context()
        assert not result["no_op"]
        assert session._context_fingerprints == {}

        request = session._build_chat_messages(
            user_content="question-3",
            system_instructions="",
        )
        update = _context_messages(request)[0]["content"]
        assert "STABLE-ENV" in update


def test_summary_input_keeps_previous_summary_and_latest_old_message():
    session = _session()
    source = [
        make_compact_boundary_message("EARLIEST-DECISION"),
        _user("A" * 2_000),
        _assistant("B" * 2_000),
        _user("LATEST-OLD-MESSAGE-" + "Z" * 500),
    ]

    with patch("agent.session.COMPACT_SOURCE_MAX_TOKENS", 120):
        text = session._history_text_for_compact(source)

    assert "EARLIEST-DECISION" in text
    assert "LATEST-OLD-MESSAGE" in text
    assert "A" * 100 not in text


def test_three_consecutive_compactions_keep_earliest_summary_content():
    session = _session()
    session.history = [
        _user("FIRST-TASK-MARKER"),
        _assistant("first-answer"),
        _user("second-question"),
        _assistant("second-answer"),
    ]

    with patch("agent.session.COMPACT_RETAINED_MESSAGE_TOKENS", 20):
        first = session.compact_context()
        assert "FIRST-TASK-MARKER" in first["summary"]

        session.history.extend([
            _user("third-question"),
            _assistant("third-answer"),
            _user("fourth-question"),
            _assistant("fourth-answer"),
        ])
        second = session.compact_context()
        assert "FIRST-TASK-MARKER" in second["summary"]

        session.history.extend([
            _user("fifth-question"),
            _assistant("fifth-answer"),
            _user("sixth-question"),
            _assistant("sixth-answer"),
        ])
        third = session.compact_context()
        assert "FIRST-TASK-MARKER" in third["summary"]


def test_manual_compact_is_noop_when_all_turns_fit_raw_budget():
    session = _session()
    session.history = [
        _user("small-question"),
        _assistant("small-answer"),
    ]
    before = [_message_to_persist_payload(message) for message in session.history]

    result = session.compact_context()

    assert result["no_op"]
    assert result["summary"] == ""
    assert [_message_to_persist_payload(message) for message in session.history] == before


def test_llm_compactor_receives_old_summary_and_8192_output_limit():
    captured: dict[str, Any] = {}

    class Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(content="【上下文压缩】new-summary"),
                )],
            )

    session = _session()
    session.llm.client = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions()),
    )
    source = [
        make_compact_boundary_message("EARLIEST-DECISION"),
        _user("old-question"),
    ]

    summary = session._make_compact_summary(messages=source, state_text="")

    assert summary == "【上下文压缩】new-summary"
    assert captured["max_tokens"] == 8192
    assert "EARLIEST-DECISION" in captured["messages"][1]["content"]


def test_compact_json_saves_full_summary_and_replacement_history():
    with tempfile.TemporaryDirectory() as td:
        store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
        summary = "【上下文压缩】" + "X" * 5_000
        boundary = make_compact_boundary_message(summary)
        retained = [_user("recent-question"), _assistant("recent-answer")]
        replacement = [boundary, *retained]

        store.save_compaction(
            summary=summary,
            history_payload=[_message_to_persist_payload(message) for message in replacement],
            before_messages=20,
            after_messages=3,
        )

        saved = json.loads((store.active_dir / "compact.json").read_text(encoding="utf-8"))
        assert saved["summary"] == summary
        assert len(saved["history"]) == 3

        restored = LocalSessionStore(store.root).load_latest_history()
        assert len(restored) == 3
        assert (restored[0].metadata or {}).get("kind") == "compact_boundary"
        assert restored[1].content == "recent-question"
