"""Work context 与 LocalSessionStore 的核心持久化测试 —— CC 模式。

重构后 transcript.jsonl 改为存原始 messages 列表(含 user / assistant 含
tool_calls / role=tool / final assistant)。这里只覆盖必须保证的几个点:

1. trace_entry_from_tool_result 仍能正确截断超大输出
2. append_turn 能够落盘 raw messages 并被 load_latest_history 还原
3. load_latest_history 能还原 assistant.tool_calls 与 role=tool
4. save_pending_user_message 配合 commit 流程不会重复
5. switch/list 多 session 隔离仍然成立
6. compact_boundary 落盘 + 恢复后切片使用
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.compact_boundary import (
    COMPACT_BOUNDARY_KIND,
    make_compact_boundary_message,
)
from agent.work_context import (
    LocalSessionStore,
    RuleTraceSummarizer,
    WorkRecord,
    _message_to_persist_payload,
    trace_entry_from_tool_result,
)
from core.message import Message


def _user(text: str) -> Message:
    return Message.create_user_message(text)


def _assistant(text: str = None, tool_calls=None) -> Message:
    return Message.create_assistant_message(input_text=text, tool_calls=tool_calls)


def _tool(call_id: str, name: str, content: str) -> Message:
    return Message.create_tool_message(
        tool_call_id=call_id,
        tool_name=name,
        tool_output=content,
    )


class TestTraceEntry(unittest.TestCase):
    def test_file_read_trace_is_clipped_and_structured(self):
        long_content = "abcdef" * 40
        entry = trace_entry_from_tool_result(
            name="file_read",
            arguments={"path": "agent/session.py"},
            result=json.dumps({
                "path": "agent/session.py",
                "mode": "head-100",
                "total_lines": 400,
                "returned_lines": 100,
                "truncated": False,
                "content": long_content,
            }, ensure_ascii=False),
            is_error=False,
            round_idx=1,
        )

        self.assertEqual(entry.name, "file_read")
        self.assertLessEqual(len(entry.result_summary), 100)
        self.assertEqual(entry.metadata["path"], "agent/session.py")
        self.assertNotIn(long_content, entry.to_line())


class TestPersistAndRestoreMessages(unittest.TestCase):
    def test_append_turn_persists_raw_messages_and_restores(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            committed = [
                _user("帮我读 a.py"),
                _assistant(tool_calls=[{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "file_read", "arguments": "{\"path\":\"a.py\"}"},
                }]),
                _tool("call_1", "file_read", json.dumps({
                    "path": "a.py", "content": "print('hello')",
                }, ensure_ascii=False)),
                _assistant("已经看完 a.py"),
            ]
            store.append_turn(
                user_query="帮我读 a.py",
                final_answer="已经看完 a.py",
                committed_messages=committed,
            )

            transcript = store.active_dir / "transcript.jsonl"
            self.assertTrue(transcript.exists())
            line = json.loads(transcript.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(len(line["messages"]), 4)
            roles = [m["role"] for m in line["messages"]]
            self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
            # tool_call_id / tool_calls 都被持久化
            self.assertEqual(line["messages"][1]["tool_calls"][0]["id"], "call_1")
            self.assertEqual(line["messages"][2]["tool_call_id"], "call_1")

            restored = LocalSessionStore(root)
            history = restored.load_latest_history(max_messages=20)
            self.assertEqual(len(history), 4)
            self.assertEqual(history[1].tool_calls[0]["id"], "call_1")
            self.assertEqual(history[2].tool_call_id, "call_1")
            self.assertEqual(history[2].tool_name, "file_read")
            self.assertEqual(history[3].content, "已经看完 a.py")

    def test_default_load_latest_history_restores_full_active_history(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            for i in range(8):
                store.append_turn(
                    user_query=f"问题 {i}",
                    final_answer=f"回答 {i}",
                    committed_messages=[_user(f"问题 {i}"), _assistant(f"回答 {i}")],
                )

            restored = LocalSessionStore(root)
            full_history = restored.load_latest_history()
            limited_history = restored.load_latest_history(max_messages=12)

            self.assertEqual(len(full_history), 16)
            self.assertEqual(len(limited_history), 12)
            text = "\n".join(str(m.content) for m in full_history)
            self.assertIn("问题 0", text)
            self.assertIn("回答 7", text)

    def test_append_turn_with_state_structured_fields(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            entry = trace_entry_from_tool_result(
                name="file_read",
                arguments={"path": "a.py"},
                result=json.dumps({"path": "a.py", "content": "x"}, ensure_ascii=False),
                is_error=False, round_idx=1,
            )
            record = RuleTraceSummarizer().summarize(
                user_query="读 a.py", final_answer="ok", trace_entries=[entry],
            )
            self.assertEqual(record.text, "")  # 已不再生成文本
            self.assertIn("a.py", record.files_seen)

            store.append_turn(
                user_query="读 a.py",
                final_answer="ok",
                committed_messages=[_user("读 a.py"), _assistant("ok")],
                work_record=record,
            )
            self.assertIn("a.py", store.state_text())

    def test_pending_user_message_restores_and_is_cleared_after_turn(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            store.append_turn(
                user_query="上一轮",
                final_answer="回完",
                committed_messages=[_user("上一轮"), _assistant("回完")],
            )
            store.save_pending_user_message("尚未回答")
            self.assertTrue((store.active_dir / "pending_user.json").exists())

            restored = LocalSessionStore(root)
            history = restored.load_latest_history(max_messages=20)
            text = "\n".join(str(m.content) for m in history)
            self.assertIn("上一轮", text)
            self.assertIn("回完", text)
            self.assertIn("尚未回答", text)

            restored.append_turn(
                user_query="尚未回答",
                final_answer="刚回",
                committed_messages=[_user("尚未回答"), _assistant("刚回")],
            )
            self.assertFalse((restored.active_dir / "pending_user.json").exists())
            final = LocalSessionStore(root).load_latest_history(max_messages=20)
            text = "\n".join(str(m.content) for m in final)
            self.assertEqual(text.count("尚未回答"), 1)
            self.assertIn("刚回", text)


class TestSessionIsolation(unittest.TestCase):
    def test_lists_creates_and_switches_isolated_sessions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            first_id = store.active_session_id
            store.append_turn(
                user_query="一号问题",
                final_answer="一号回答",
                committed_messages=[_user("一号问题"), _assistant("一号回答")],
            )

            second = store.create_session()
            second_id = second["session_id"]
            self.assertNotEqual(first_id, second_id)
            store.append_turn(
                user_query="二号问题",
                final_answer="二号回答",
                committed_messages=[_user("二号问题"), _assistant("二号回答")],
            )

            sessions = {s["session_id"] for s in store.list_sessions()}
            self.assertEqual(sessions, {first_id, second_id})

            store.switch_session(first_id)  # type: ignore[arg-type]
            history = store.load_latest_history(max_messages=20)
            text = "\n".join(str(m.content) for m in history)
            self.assertIn("一号问题", text)
            self.assertNotIn("二号问题", text)

            with self.assertRaises(ValueError):
                store.switch_session("../outside")


class TestCompactBoundaryPersistence(unittest.TestCase):
    def test_save_compaction_with_boundary_payload_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            store.append_turn(
                user_query="旧一",
                final_answer="老答一",
                committed_messages=[_user("旧一"), _assistant("老答一")],
            )
            store.append_turn(
                user_query="旧二",
                final_answer="老答二",
                committed_messages=[_user("旧二"), _assistant("老答二")],
            )

            boundary = make_compact_boundary_message("摘要:已读 a.py")
            store.save_compaction(
                summary=str(boundary.content or ""),
                history_payload=[_message_to_persist_payload(boundary)],
                before_messages=4,
                after_messages=1,
            )

            self.assertTrue((store.active_dir / "compact.json").exists())
            self.assertTrue((store.active_dir / "compactions.jsonl").exists())

            store.append_turn(
                user_query="新一",
                final_answer="新答一",
                committed_messages=[_user("新一"), _assistant("新答一")],
            )

            restored = LocalSessionStore(root)
            history = restored.load_latest_history(max_messages=20)
            # 第一条应是 boundary
            self.assertEqual(
                (history[0].metadata or {}).get("kind"),
                COMPACT_BOUNDARY_KIND,
            )
            text = "\n".join(str(m.content) for m in history)
            self.assertIn("【上下文压缩】", text)
            self.assertIn("新一", text)
            # boundary 之前的旧消息不再注入
            self.assertNotIn("旧一", text)
            self.assertNotIn("老答一", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
