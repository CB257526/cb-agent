from __future__ import annotations

import logging
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

from agent.logging_config import normalize_log_verbosity, resolve_logging_settings
from agent.message_logger import MessageLogger


class TestLoggingConfig(unittest.TestCase):
    def test_normalize_log_verbosity(self) -> None:
        self.assertEqual(normalize_log_verbosity(None), "basic")
        self.assertEqual(normalize_log_verbosity("DETAIL"), "detail")
        self.assertEqual(normalize_log_verbosity("trace"), "full")
        self.assertEqual(normalize_log_verbosity("unknown"), "basic")

    def test_resolve_settings_for_three_levels(self) -> None:
        root = Path("C:/repo")

        basic = resolve_logging_settings(
            project_root=root,
            env={"CBAGENT_LOG_LEVEL": "basic"},
            timestamp=123,
        )
        self.assertEqual(basic.verbosity, "basic")
        self.assertEqual(basic.message_log_mode, "full")
        self.assertEqual(basic.console_level, logging.WARNING)
        self.assertEqual(basic.system_log_dir, root / ".cbagent" / "logs" / "system")
        self.assertEqual(
            basic.conversation_log_dir,
            root / ".cbagent" / "logs" / "conversations",
        )

        detail = resolve_logging_settings(
            project_root=root,
            env={"CBAGENT_LOG_LEVEL": "detail"},
            timestamp=123,
        )
        self.assertEqual(detail.verbosity, "detail")
        self.assertEqual(detail.message_log_mode, "full")
        self.assertEqual(detail.project_level, logging.DEBUG)

        full = resolve_logging_settings(
            project_root=root,
            env={"CBAGENT_LOG_LEVEL": "full"},
            timestamp=123,
        )
        self.assertEqual(full.verbosity, "full")
        self.assertEqual(full.message_log_mode, "full")
        self.assertEqual(full.third_party_level, logging.DEBUG)

    def test_relative_log_dir_resolves_under_project_root(self) -> None:
        root = Path("C:/repo")
        settings = resolve_logging_settings(
            project_root=root,
            env={
                "CBAGENT_LOG_LEVEL": "detail",
                "CBAGENT_LOG_DIR": "logs/custom",
            },
            timestamp=456,
        )

        self.assertEqual(settings.log_dir, root / "logs" / "custom")
        self.assertEqual(settings.system_log_dir, root / "logs" / "custom" / "system")
        self.assertEqual(settings.runtime_log_path, settings.system_log_dir / "cb-agent-456.log")


class TestMessageLogger(unittest.TestCase):
    def test_full_mode_keeps_content(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "messages.log"
            logger = MessageLogger(path, mode="full")
            try:
                logger.log([
                    {"role": "user", "content": "full-content-body"},
                ], label="test")
            finally:
                logger.close()

            text = path.read_text(encoding="utf-8")

        self.assertIn('"mode": "full"', text)
        self.assertIn("full-content-body", text)

    def test_full_mode_records_structured_messages_without_clipping(self) -> None:
        long_text = "prompt-" + ("x" * 5000)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "messages.log"
            logger = MessageLogger(path, mode="full")
            try:
                logger.log(
                    [
                        {"role": "user", "content": long_text},
                        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
                        {"role": "tool", "tool_call_id": "c1", "name": "bash", "content": long_text},
                    ],
                    label="round",
                    tools=[{"type": "function", "function": {"name": "bash"}}],
                    response={"answer": "ok"},
                )
            finally:
                logger.close()

            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

        event = records[1]
        self.assertEqual(event["event"], "messages")
        self.assertEqual(event["message_count"], 3)
        self.assertEqual(event["messages"][0]["content"], long_text)
        self.assertEqual(event["messages"][2]["content"], long_text)
        self.assertEqual(event["tools"][0]["function"]["name"], "bash")
        self.assertEqual(event["response"]["answer"], "ok")

    def test_non_full_mode_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                MessageLogger(Path(td) / "messages.log", mode="summary")


if __name__ == "__main__":
    unittest.main(verbosity=2)
