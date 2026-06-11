"""Work context compression and local session store tests."""

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

from agent.work_context import (
    LocalSessionStore,
    RuleTraceSummarizer,
    make_compact_record_message,
    trace_entry_from_tool_result,
)


class TestWorkContext(unittest.TestCase):
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

    def test_local_session_store_persists_restores_and_clears(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            entry = trace_entry_from_tool_result(
                name="file_read",
                arguments={"path": "a.py"},
                result=json.dumps({
                    "path": "a.py",
                    "mode": "range-1-10",
                    "total_lines": 10,
                    "returned_lines": 10,
                    "truncated": False,
                    "content": "print('hello')",
                }, ensure_ascii=False),
                is_error=False,
                round_idx=1,
            )
            record = RuleTraceSummarizer().summarize(
                user_query="读 a.py",
                final_answer="看完了",
                trace_entries=[entry],
            )

            store.append_turn(
                user_query="读 a.py",
                final_answer="看完了",
                work_record=record,
            )

            transcript = store.active_dir / "transcript.jsonl"
            state = store.active_dir / "state.json"
            self.assertTrue(transcript.exists())
            self.assertTrue(state.exists())
            raw = transcript.read_text(encoding="utf-8")
            self.assertIn("【工作记录】", raw)
            self.assertNotIn("abcdef" * 40, raw)

            restored = LocalSessionStore(root)
            history = restored.load_latest_history()
            self.assertEqual(len(history), 3)
            self.assertIn("【工作记录】", str(history[-1].content))
            self.assertIn("a.py", restored.state_text())

            restored.clear_active_session()
            self.assertFalse((root / "index.json").exists())
            self.assertFalse(transcript.exists())

    def test_local_session_store_can_skip_trace_entries_for_platform_chat(self):
        """通讯平台私聊保留压缩工作记录，但不落完整工具明细。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root, persist_trace_entries=False)
            entry = trace_entry_from_tool_result(
                name="bash",
                arguments={"command": "echo should-not-persist"},
                result=json.dumps({
                    "command": "echo should-not-persist",
                    "exit_code": 0,
                    "stdout": "工具输出不该进入 QQ 私聊长期上下文",
                }, ensure_ascii=False),
                is_error=False,
                round_idx=1,
            )
            record = RuleTraceSummarizer().summarize(
                user_query="干净用户原话",
                final_answer="干净最终回复",
                trace_entries=[entry],
            )

            store.append_turn(
                user_query="干净用户原话",
                final_answer="干净最终回复",
                work_record=record,
            )

            transcript = store.active_dir / "transcript.jsonl"
            raw = transcript.read_text(encoding="utf-8")
            payload = json.loads(raw.splitlines()[0])
            self.assertEqual(payload["user_query"], "干净用户原话")
            self.assertEqual(payload["final_answer"], "干净最终回复")
            self.assertIn("【工作记录】", payload["work_record"])
            self.assertEqual(payload["trace_entries"], [])
            self.assertIn("【工作记录】", store.state_text())

            # 兼容旧版本已经写入的 trace_entries：恢复时仍只使用压缩 work_record，
            # 不把逐工具明细还原成 OpenAI tool 协议或普通上下文。
            old_item = {
                "ts": "2026-06-07T00:00:00+00:00",
                "user_query": "旧用户问题",
                "final_answer": "旧助手回答",
                "work_record": "【工作记录】调用工具：fetch_fetch 旧工具流水账",
                "trace_entries": [entry.to_dict()],
            }
            with transcript.open("a", encoding="utf-8") as f:
                f.write(json.dumps(old_item, ensure_ascii=False) + "\n")

            restored = LocalSessionStore(root, persist_trace_entries=False)
            history = restored.load_latest_history(max_messages=10)
            restored_text = "\n".join(str(m.content) for m in history)
            self.assertIn("干净用户原话", restored_text)
            self.assertIn("干净最终回复", restored_text)
            self.assertIn("旧用户问题", restored_text)
            self.assertIn("旧助手回答", restored_text)
            self.assertIn("【工作记录】", restored_text)
            self.assertIn("fetch_fetch", restored_text)

    def test_pending_user_message_restores_and_is_cleared_after_turn(self):
        """收到用户消息后先落 pending；完整回合落盘后再清理，避免重复历史。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            store.append_turn(
                user_query="上一轮问题",
                final_answer="上一轮回答",
                work_record=None,
            )
            store.save_pending_user_message("这条消息已经收到但还没回答")
            pending_path = store.active_dir / "pending_user.json"
            self.assertTrue(pending_path.exists())

            restored = LocalSessionStore(root)
            restored_history = restored.load_latest_history(max_messages=10)
            restored_text = "\n".join(str(m.content) for m in restored_history)
            self.assertIn("上一轮问题", restored_text)
            self.assertIn("上一轮回答", restored_text)
            self.assertIn("这条消息已经收到但还没回答", restored_text)

            restored.append_turn(
                user_query="这条消息已经收到但还没回答",
                final_answer="现在回答完成",
                work_record=None,
            )
            self.assertFalse((restored.active_dir / "pending_user.json").exists())

            final_restore = LocalSessionStore(root)
            final_history = final_restore.load_latest_history(max_messages=10)
            final_text = "\n".join(str(m.content) for m in final_history)
            self.assertEqual(final_text.count("这条消息已经收到但还没回答"), 1)
            self.assertIn("现在回答完成", final_text)

    def test_local_session_store_lists_creates_and_switches_isolated_sessions(self):
        """多个 session 目录互相隔离，切换只恢复目标目录自己的 transcript/state。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            first_id = store.active_session_id
            self.assertIsNotNone(first_id)
            store.append_turn(
                user_query="第一会话的问题",
                final_answer="第一会话的回答",
                work_record=None,
            )

            second_summary = store.create_session()
            second_id = second_summary["session_id"]
            self.assertNotEqual(first_id, second_id)
            store.append_turn(
                user_query="第二会话的问题",
                final_answer="第二会话的回答",
                work_record=None,
            )

            sessions = store.list_sessions()
            self.assertEqual({s["session_id"] for s in sessions}, {first_id, second_id})
            self.assertEqual(
                [s for s in sessions if s["is_active"]][0]["session_id"],
                second_id,
            )

            switched = store.switch_session(first_id)  # type: ignore[arg-type]
            self.assertEqual(switched["session_id"], first_id)
            history = store.load_latest_history(max_messages=10)
            restored_text = "\n".join(str(m.content) for m in history)
            self.assertIn("第一会话的问题", restored_text)
            self.assertIn("第一会话的回答", restored_text)
            self.assertNotIn("第二会话的问题", restored_text)

            index = json.loads((root / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(index["active_session_id"], first_id)

            with self.assertRaises(ValueError):
                store.switch_session("../outside")

    def test_compaction_snapshot_restores_from_anchor_and_keeps_transcript(self):
        """compact 后保留 transcript 审计，但恢复 history 时从 compact 锚点继续。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)

            store.append_turn(
                user_query="旧问题一",
                final_answer="旧回答一",
                work_record=None,
            )
            store.append_turn(
                user_query="旧问题二",
                final_answer="旧回答二",
                work_record=None,
            )
            transcript = store.active_dir / "transcript.jsonl"
            raw_before = transcript.read_text(encoding="utf-8")

            compact_msg = make_compact_record_message("【上下文压缩】旧上下文已经压缩")
            recent_user = {"role": "user", "content": "旧问题二", "kind": None}
            recent_assistant = {"role": "assistant", "content": "旧回答二", "kind": None}
            store.save_compaction(
                summary=str(compact_msg.content),
                history_payload=[
                    {"role": "assistant", "content": str(compact_msg.content), "kind": "compact_record"},
                    recent_user,
                    recent_assistant,
                ],
                before_messages=4,
                after_messages=3,
            )

            self.assertTrue((store.active_dir / "compact.json").exists())
            self.assertTrue((store.active_dir / "compactions.jsonl").exists())
            self.assertEqual(raw_before, transcript.read_text(encoding="utf-8"))

            store.append_turn(
                user_query="compact 后的新问题",
                final_answer="新回答",
                work_record=None,
            )

            restored = LocalSessionStore(root)
            history = restored.load_latest_history(max_messages=12)
            restored_text = "\n".join(str(m.content) for m in history)
            self.assertIn("【上下文压缩】", restored_text)
            self.assertIn("旧问题二", restored_text)
            self.assertIn("compact 后的新问题", restored_text)
            self.assertNotIn("旧问题一", restored_text)

    def test_compaction_snapshot_rolls_back_when_state_save_fails(self):
        """compact 落盘中途失败时，不留下半套 compact 快照。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            store.append_turn(
                user_query="旧问题",
                final_answer="旧回答",
                work_record=None,
            )
            state_path = store.active_dir / "state.json"
            state_before = state_path.read_text(encoding="utf-8")

            def fail_save_state(state):
                raise OSError("state write failed")

            store.save_state = fail_save_state  # type: ignore[method-assign]

            with self.assertRaises(OSError):
                store.save_compaction(
                    summary="【上下文压缩】旧上下文已经压缩",
                    history_payload=[
                        {
                            "role": "assistant",
                            "content": "【上下文压缩】旧上下文已经压缩",
                            "kind": "compact_record",
                        },
                    ],
                    before_messages=2,
                    after_messages=1,
                )

            self.assertFalse((store.active_dir / "compact.json").exists())
            self.assertFalse((store.active_dir / "compactions.jsonl").exists())
            self.assertEqual(state_before, state_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
