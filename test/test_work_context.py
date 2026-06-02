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


if __name__ == "__main__":
    unittest.main(verbosity=2)
