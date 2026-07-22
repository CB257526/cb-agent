"""Codex 风格结构化 compact 与 world state 回归测试。"""

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

from agent.compaction import (
    COMPACTION_SUMMARY_KIND,
    SUMMARY_PREFIX,
    SUMMARIZATION_PROMPT,
    CompactionError,
    dynamic_retained_token_target,
)
from agent.event_bus import EventBus
from agent.executor import ToolExecutor
from agent.session import AgentSession
from agent.work_context import LocalSessionStore
from constant.llm.constant_llm import ConstantLLM
from context.world_state import WorldStateSnapshot
from core.message import Message, MessageRole


class FakeCompletions:
    """记录静默摘要请求并返回可配置结果。"""

    def __init__(self, owner: "FakeLLM") -> None:
        self.owner = owner

    def create(self, **kwargs):
        self.owner.compact_calls.append(kwargs)
        if self.owner.compact_error is not None:
            raise self.owner.compact_error
        summary = self.owner.compact_summaries.pop(0) if self.owner.compact_summaries else "handoff"
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=summary))]
        )


class FakeLLM:
    """同时支持普通 think 与非流式 compact 请求。"""

    def __init__(self, answers: list[str] | None = None, summaries: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.compact_summaries = list(summaries or [])
        self.calls: list[dict[str, Any]] = []
        self.compact_calls: list[dict[str, Any]] = []
        self.compact_error: Exception | None = None
        self.is_Function_Calling = True
        self.model = "fake"
        self.provider = "fake-provider"
        self.max_output_tokens = 4096
        self.output_token_param = "max_tokens"
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions(self))
        )

    def think(self, messages, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools})
        answer = self.answers.pop(0) if self.answers else "ok"
        return {"answer": answer, "tool_calls": []}

    def _apply_output_token_limit(self, request_kwargs):
        if self.output_token_param == "none":
            return
        request_kwargs[self.output_token_param] = self.max_output_tokens


class FakeRegistry:
    """提供稳定的最小工具注册表。"""

    def list_tools(self):
        return []

    def get_tools_description_openai_schema(self):
        return []


def _session(*, store: LocalSessionStore | None = None, answers=None, summaries=None) -> AgentSession:
    bus = EventBus()
    llm = FakeLLM(answers, summaries)
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


def _context_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message for message in messages
        if message.get("role") == "user"
        and "<context-update>" in str(message.get("content") or "")
    ]


def test_world_state_diff_only_emits_changed_and_removed_sections():
    current = [("environment", "ENV-A"), ("knowledge", "KNOWLEDGE-A")]

    async def dynamic_sections(**_kwargs):
        return list(current)

    session = _session()
    with patch("agent.session.get_dynamic_context_sections", new=dynamic_sections):
        first = session._build_chat_messages(user_content="first", system_instructions="")
        assert "ENV-A" in _context_messages(first)[0]["content"]
        assert "KNOWLEDGE-A" in _context_messages(first)[0]["content"]

        session._world_state_baseline = session._pending_world_state
        second = session._build_chat_messages(user_content="second", system_instructions="")
        # knowledge 与查询绑定，即使稳定也必须作为本轮临时上下文重新出现。
        assert "KNOWLEDGE-A" in _context_messages(second)[0]["content"]
        assert "ENV-A" not in _context_messages(second)[0]["content"]

        current[:] = [("environment", "ENV-B")]
        third = session._build_chat_messages(user_content="third", system_instructions="")
        update = _context_messages(third)[0]["content"]
        assert "ENV-B" in update
        assert "KNOWLEDGE-A" not in update


def test_restart_recovers_actual_world_state_snapshot():
    async def dynamic_sections(**_kwargs):
        return [("environment", "STABLE-ENV")]

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / ".cbagent" / "sessions"
        with (
            patch("agent.session.get_dynamic_context_sections", new=dynamic_sections),
            patch.object(AgentSession, "_build_system_instructions", return_value=""),
        ):
            first = _session(store=LocalSessionStore(root), answers=["first-answer"])
            first.chat("first-question")
            assert first._world_state_baseline.sections["environment"] == "STABLE-ENV"

            restarted = _session(store=LocalSessionStore(root))
            assert restarted._world_state_baseline == first._world_state_baseline
            request = restarted._build_chat_messages(
                user_content="second-question",
                system_instructions="",
            )
            updates = _context_messages(request)
            # 第一条来自已提交历史；第二条反映上一轮结束后 state.json 新增的当前任务。
            assert len(updates) == 2
            assert "当前任务：first-question" in updates[-1]["content"]


def test_structured_compact_request_keeps_protocol_messages_and_appends_prompt():
    session = _session(summaries=["structured handoff"])
    session.history = [
        _user("inspect repository"),
        Message.create_assistant_message(
            tool_calls=[{
                "id": "call-1",
                "type": "function",
                "function": {"name": "file_read", "arguments": '{"path":"a.py"}'},
            }]
        ),
        Message.create_tool_message("call-1", "file_read", "FILE-MARKER"),
        _assistant("finished inspection"),
    ]

    result = session.compact_context(reason="auto")

    request = session.llm.compact_calls[0]["messages"]
    assert session.llm.compact_calls[0]["max_tokens"] == 4096
    assert request[-1] == {"role": "user", "content": SUMMARIZATION_PROMPT}
    assert any(message.get("tool_call_id") == "call-1" for message in request)
    assert any(message.get("content") == "FILE-MARKER" for message in request)
    assert request[-2]["content"] == "finished inspection"
    assert result["summary"].startswith(SUMMARY_PREFIX)
    assert (session.history[-1].metadata or {}).get("kind") == COMPACTION_SUMMARY_KIND


def test_compact_respects_none_output_token_param():
    """provider 配置为 none 时，摘要请求只做预算预留，不发送输出限制字段。"""

    session = _session(summaries=["handoff"])
    session.llm.output_token_param = "none"
    session.history = [_user("task"), _assistant("progress")]

    session.compact_context(reason="auto")

    request = session.llm.compact_calls[0]
    assert "max_tokens" not in request
    assert "max_completion_tokens" not in request


def test_consecutive_compactions_send_previous_handoff_as_structured_history():
    session = _session(summaries=["FIRST-HANDOFF", "SECOND-HANDOFF"])
    session.history = [_user("FIRST-TASK"), _assistant("first-result")]
    session.compact_context(reason="auto")
    session.history.extend([_user("SECOND-TASK"), _assistant("second-result")])
    session.compact_context(reason="auto")

    second_request = session.llm.compact_calls[1]["messages"]
    assert any(
        SUMMARY_PREFIX in str(message.get("content") or "")
        and "FIRST-HANDOFF" in str(message.get("content") or "")
        for message in second_request
    )
    assert any(message.get("content") == "SECOND-TASK" for message in second_request)


def test_manual_compact_resets_baseline_and_next_turn_reinjects_full_state():
    async def dynamic_sections(**_kwargs):
        return [("environment", "STABLE-ENV")]

    session = _session(summaries=["handoff"])
    session.history = [_user("Q" * 20000), _assistant("A" * 20000)]
    session._world_state_baseline = WorldStateSnapshot.from_sections([
        ("environment", "STABLE-ENV")
    ])
    with (
        patch("agent.session.get_dynamic_context_sections", new=dynamic_sections),
        patch("agent.session.dynamic_retained_token_target", return_value=100),
    ):
        result = session.compact_context(reason="user_compact")
        assert not result["no_op"]
        assert session._world_state_baseline.sections == {}
        request = session._build_chat_messages(user_content="next", system_instructions="")
        assert "STABLE-ENV" in _context_messages(request)[0]["content"]


def test_mid_turn_compact_installs_full_world_state_before_summary():
    session = _session(summaries=["handoff"])
    session.history = [_user("task"), _assistant("progress")]
    session._pending_world_state = WorldStateSnapshot.from_sections([
        ("environment", "ENV-NOW"),
        ("plan", "PLAN-NOW"),
    ])

    result = session.compact_context(reason="mid_turn")

    assert result["world_state_sections"] == 2
    assert session._world_state_baseline.sections["environment"] == "ENV-NOW"
    assert (session.history[-1].metadata or {}).get("kind") == COMPACTION_SUMMARY_KIND
    context_message = next(
        message for message in session.history
        if (message.metadata or {}).get("kind") == "context_update"
    )
    assert "ENV-NOW" in str(context_message.content)
    assert session.history.index(context_message) < len(session.history) - 1


def test_compact_failure_keeps_history_and_snapshot_unchanged():
    session = _session()
    session.history = [_user("task"), _assistant("progress")]
    session._world_state_baseline = WorldStateSnapshot.from_sections([("environment", "ENV")])
    original_history = [message.model_copy(deep=True) for message in session.history]
    session.llm.compact_error = RuntimeError("network unavailable")

    try:
        session.compact_context(reason="auto")
    except CompactionError:
        pass
    else:
        raise AssertionError("compact 应该失败")

    assert session.history == original_history
    assert session._world_state_baseline.sections == {"environment": "ENV"}


def test_compact_v2_persists_replacement_history_and_world_state():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / ".cbagent" / "sessions"
        store = LocalSessionStore(root)
        session = _session(store=store, summaries=["persisted handoff"])
        session.history = [_user("task"), _assistant("progress")]
        session._pending_world_state = WorldStateSnapshot.from_sections([("environment", "ENV")])

        session.compact_context(reason="mid_turn")

        compact = json.loads((store.active_dir / "compact.json").read_text(encoding="utf-8"))
        # compact 快照升级为 v3：用 transcript_cursor_seq 替代列表下标 offset。
        assert compact["version"] == 3
        assert "transcript_cursor_seq" in compact
        assert compact["world_state_snapshot"] == {"environment": "ENV"}
        assert compact["replacement_history"][-1]["kind"] == COMPACTION_SUMMARY_KIND
        restored = LocalSessionStore(root).load_latest_history()
        assert (restored[-1].metadata or {}).get("kind") == COMPACTION_SUMMARY_KIND


def test_legacy_compact_snapshot_is_ignored():
    """破坏性升级后旧 compact 快照不得覆盖 transcript 事实。"""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / ".cbagent" / "sessions"
        store = LocalSessionStore(root)
        store.append_turn(
            user_query="真实问题",
            final_answer="真实回答",
            committed_messages=[_user("真实问题"), _assistant("真实回答")],
        )
        legacy = {
            "summary": "LEGACY-SUMMARY",
            "history": [{"role": "user", "content": "LEGACY-HISTORY"}],
            "transcript_offset": 1,
        }
        (store.active_dir / "compact.json").write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )

        restored = LocalSessionStore(root).load_latest_history()
        text = "\n".join(str(message.content) for message in restored)
        assert "真实问题" in text
        assert "LEGACY-HISTORY" not in text


def test_dynamic_retained_budget_uses_ten_percent_with_bounds():
    assert dynamic_retained_token_target(128_000) == 16 * 1024
    assert dynamic_retained_token_target(400_000) == 40_000
    assert dynamic_retained_token_target(1_000_000) == 100_000
    assert dynamic_retained_token_target(2_000_000) == 128 * 1024


def test_model_downshift_uses_target_window_for_replacement_budget():
    """旧大模型负责摘要，replacement 必须按目标小模型窗口收紧。"""

    original = ConstantLLM.llm_dict.get("small-target")
    ConstantLLM.llm_dict["small-target"] = {
        "is_tool": True,
        "is_reasoning": False,
        "max_tokens": 20_000,
        "max_output_tokens": 2_000,
    }
    try:
        session = _session(summaries=["downshift handoff"])
        session.history = [
            _user(f"task-{index}-" + "word " * 4000)
            if index % 2 == 0 else _assistant(f"answer-{index}-" + "word " * 4000)
            for index in range(8)
        ]

        result = session.compact_context(
            reason="model_downshift",
            target_model="small-target",
        )

        assert result["target_model"] == "small-target"
        assert result["retained_target_tokens"] == 16 * 1024
        messages, tools = session._baseline_request_parts()
        assert session._estimate_request_tokens(messages, tools) <= 16_000
    finally:
        if original is None:
            ConstantLLM.llm_dict.pop("small-target", None)
        else:
            ConstantLLM.llm_dict["small-target"] = original
