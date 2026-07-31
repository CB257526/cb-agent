import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.history_journal import HistoryJournal
from agent.legacy_history_migrator import load_legacy_history
from agent.work_context import (
    LocalSessionStore,
    TraceStateIndexer,
    TraceCollector,
    trace_entry_from_tool_result,
)
from core.conversation_history import ConversationHistory
from core.message import Message


def test_trace_result_is_bounded_without_rewriting_tool_content():
    raw = json.dumps({"path": "a.py", "content": "x" * 20_000})
    entry = trace_entry_from_tool_result(
        name="file_read",
        arguments={"path": "a.py"},
        result=raw,
        is_error=False,
        round_idx=1,
        result_limit=80,
    )
    assert entry.arguments == {"path": "a.py"}
    assert len(entry.result_summary) <= 80
    assert len(entry.metadata["content_preview"]) <= 80
    assert "x" * 500 in raw


def test_trace_collector_updates_structured_state():
    collector = TraceCollector()
    collector.add_tool_result(
        call={
            "id": "call-1",
            "type": "function",
            "function": {"name": "file_read", "arguments": '{"path":"a.py"}'},
        },
        name="file_read",
        result=json.dumps({
            "path": "a.py",
            "mode": "range",
            "content": "hello",
            "returned_lines": 1,
        }),
        is_error=False,
        round_idx=1,
    )
    record = TraceStateIndexer().summarize(
        user_query="读文件",
        final_answer="完成",
        trace_entries=collector.entries,
    )
    assert "a.py" in record.files_seen


def test_session_store_isolates_state_and_usage():
    with tempfile.TemporaryDirectory() as td:
        store = LocalSessionStore(Path(td) / "sessions")
        first_id = store.active_session_id
        store.commit_turn_state(user_query="first")
        store.add_token_usage(SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            cached_prompt_tokens=70,
            prompt_cache_miss_tokens=None,
        ))
        assert store.load_usage()["cache_miss_tokens"] == 30

        second = store.create_session()
        second_id = second["session_id"]
        assert second_id != first_id
        assert store.load_usage()["requests"] == 0
        assert store.state["turn_count"] == 0

        store.switch_session(str(first_id))
        assert store.state["turn_count"] == 1
        assert store.load_usage()["prompt_tokens"] == 100


def test_session_store_clear_removes_only_active_session():
    with tempfile.TemporaryDirectory() as td:
        store = LocalSessionStore(Path(td) / "sessions")
        first = Path(store.active_dir)
        second_id = store.create_session()["session_id"]
        second = Path(store.active_dir)
        store.clear_active_session()
        assert first.exists()
        assert not second.exists()
        assert store.active_session_id is None
        assert second_id not in {item["session_id"] for item in store.list_sessions()}


def test_token_calibration_is_persisted_by_provider_model_key():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "sessions"
        store = LocalSessionStore(root)
        store.save_token_calibration("provider|model-a", 0.91, 3)
        restarted = LocalSessionStore(root)
        assert restarted.load_token_calibration("provider|model-a") == 0.91
        assert restarted.load_token_calibration("provider|model-b") is None


def test_history_journal_round_trip_and_generation_replace():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        journal = HistoryJournal(lambda: root)
        history = ConversationHistory()
        journal.append(
            history,
            [Message(role="user", content="问题")],
            turn_id="turn-a",
            event_kind="turn_input",
        )
        journal.append(
            history,
            [Message.create_assistant_message("回答")],
            turn_id="turn-a",
            event_kind="assistant",
        )
        journal.replace(
            history,
            [Message(role="user", content="摘要")],
            reason="manual",
        )
        assert history.generation == 1

        recovered = HistoryJournal(lambda: root).recover()
        assert recovered.history.generation == 1
        assert [message.content for message in recovered.history] == ["摘要"]


def test_canonical_history_rejects_append_after_old_message_mutation():
    """旧消息一旦被原地改写，下一次 journal 事务必须在写盘前失败。"""

    history = ConversationHistory()
    history.append_batch([Message(role="user", content="original")], turn_id="a")
    history[0].content = "mutated"

    with pytest.raises(RuntimeError, match="消息被改写"):
        history.prepare_batch([Message.create_assistant_message("next")], turn_id="a")


def test_history_journal_recovers_completed_and_unknown_tools_in_call_order():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        journal = HistoryJournal(lambda: root)
        history = ConversationHistory()
        calls = [
            {
                "id": "call-a",
                "type": "function",
                "function": {"name": "read", "arguments": "{}"},
            },
            {
                "id": "call-b",
                "type": "function",
                "function": {"name": "write", "arguments": "{}"},
            },
        ]
        journal.append(history, [Message(role="user", content="执行")], turn_id="turn-a")
        journal.append(
            history,
            [Message.create_assistant_message(tool_calls=calls)],
            turn_id="turn-a",
        )
        journal.checkpoint_tool_result(
            history,
            Message.create_tool_message("call-a", "read", "done"),
            turn_id="turn-a",
        )

        recovered = HistoryJournal(lambda: root).recover()
        tools = [message for message in recovered.history if message.role.value == "tool"]
        assert [message.tool_call_id for message in tools] == ["call-a", "call-b"]
        assert tools[0].content == "done"
        assert tools[1].is_error is True
        assert "禁止自动重放" in str(tools[1].content)


def test_history_journal_separates_partial_tail_before_next_event():
    """崩溃留下的半条 JSON 不能吞掉恢复后追加的新事件。"""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        journal = HistoryJournal(lambda: root)
        history = ConversationHistory()
        journal.append(history, [Message(role="user", content="first")], turn_id="a")
        with journal.path.open("ab") as handle:
            handle.write(b'{"version":4,"type":"append"')

        restarted = HistoryJournal(lambda: root)
        recovery = restarted.recover()
        assert any("corrupt_line" in warning for warning in recovery.warnings)
        restarted.append(
            recovery.history,
            [Message.create_assistant_message("second")],
            turn_id="a",
        )

        recovered_again = HistoryJournal(lambda: root).recover()
        assert [message.content for message in recovered_again.history] == [
            "first",
            "second",
        ]


def test_history_journal_does_not_reuse_completed_tool_checkpoint():
    """call id 被未来调用复用时，不得套用上一轮已经提交的工具终态。"""

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        journal = HistoryJournal(lambda: root)
        history = ConversationHistory()
        call = {
            "id": "reused-call",
            "type": "function",
            "function": {"name": "read", "arguments": "{}"},
        }
        journal.append(
            history,
            [Message.create_assistant_message(tool_calls=[call])],
            turn_id="old-turn",
        )
        journal.checkpoint_tool_result(
            history,
            Message.create_tool_message("reused-call", "read", "old-result"),
            turn_id="old-turn",
        )
        journal.append(
            history,
            [Message.create_tool_message("reused-call", "read", "old-result")],
            turn_id="old-turn",
        )
        journal.append(
            history,
            [Message.create_assistant_message(tool_calls=[call])],
            turn_id="new-turn",
        )

        recovered = HistoryJournal(lambda: root).recover()
        tools = [message for message in recovered.history if message.role.value == "tool"]
        assert len(tools) == 2
        assert tools[0].content == "old-result"
        assert "禁止自动重放" in str(tools[1].content)


def test_legacy_v3_migration_uses_turn_seq_cursor_once():
    with tempfile.TemporaryDirectory() as td:
        session_dir = Path(td)
        compact = {
            "version": 3,
            "transcript_cursor_seq": 2,
            "replacement_history": [
                {"role": "user", "content": "compact-summary", "kind": "context_compaction"}
            ],
        }
        (session_dir / "compact.json").write_text(json.dumps(compact), encoding="utf-8")
        records = [
            {
                "turn_seq": 1,
                "turn_id": "old",
                "messages": [{"role": "user", "content": "old-question"}],
            },
            {
                "turn_seq": 3,
                "turn_id": "new",
                "messages": [
                    {"role": "user", "content": "new-question"},
                    {"role": "assistant", "content": "new-answer"},
                ],
            },
        ]
        (session_dir / "transcript.jsonl").write_text(
            "\n".join(json.dumps(item) for item in records) + "\n",
            encoding="utf-8",
        )

        migrated = load_legacy_history(session_dir)
        text = "\n".join(str(message.content) for message in migrated)
        assert "compact-summary" in text
        assert "new-question" in text
        assert "old-question" not in text
        assert all(
            (message.metadata or {}).get("turn_id") == "new"
            for message in migrated[1:]
        )


def test_legacy_migration_drops_only_orphan_tool_results():
    """旧窗口裁剪产生的孤儿只允许在一次性迁移边界被清理。"""

    with tempfile.TemporaryDirectory() as td:
        session_dir = Path(td)
        record = {
            "turn_seq": 1,
            "turn_id": "legacy-turn",
            "messages": [
                {
                    "role": "tool",
                    "content": "orphan",
                    "tool_call_id": "call-orphan",
                    "name": "file_read",
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-valid",
                        "type": "function",
                        "function": {"name": "file_read", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool",
                    "content": "valid",
                    "tool_call_id": "call-valid",
                    "name": "file_read",
                },
            ],
        }
        (session_dir / "transcript.jsonl").write_text(
            json.dumps(record) + "\n",
            encoding="utf-8",
        )

        migrated = load_legacy_history(session_dir)
        tool_ids = [
            message.tool_call_id
            for message in migrated
            if message.role.value == "tool"
        ]
        assert tool_ids == ["call-valid"]


def test_legacy_migration_marks_missing_tool_result_unknown():
    """旧 committed history 的缺失工具结果必须补终态，禁止重放。"""

    with tempfile.TemporaryDirectory() as td:
        session_dir = Path(td)
        record = {
            "turn_seq": 1,
            "turn_id": "legacy-turn",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-missing",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }],
                },
                {"role": "assistant", "content": "旧记录提前进入下一条消息"},
            ],
        }
        (session_dir / "transcript.jsonl").write_text(
            json.dumps(record) + "\n",
            encoding="utf-8",
        )

        migrated = load_legacy_history(session_dir)
        assert [message.role.value for message in migrated] == [
            "assistant",
            "tool",
            "assistant",
        ]
        assert migrated[1].tool_call_id == "call-missing"
        assert migrated[1].is_error is True
        assert '"status": "unknown"' in str(migrated[1].content)
