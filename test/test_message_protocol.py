"""Canonical history 工具协议的严格校验测试。"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.message_protocol import validate_tool_protocol


def _assistant_calls(*call_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "file_read", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def _tool_result(call_id: str) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": "file_read",
        "content": "ok",
    }


class TestValidateToolProtocol(unittest.TestCase):
    def test_complete_multi_tool_block_is_valid(self):
        messages = [
            {"role": "user", "content": "读取文件"},
            _assistant_calls("call-a", "call-b"),
            _tool_result("call-a"),
            _tool_result("call-b"),
            {"role": "assistant", "content": "完成"},
        ]
        validate_tool_protocol(messages)

    def test_orphan_tool_is_rejected_without_mutating_input(self):
        messages = [_tool_result("orphan"), {"role": "user", "content": "继续"}]
        before = [dict(message) for message in messages]
        with self.assertRaisesRegex(ValueError, "没有父"):
            validate_tool_protocol(messages)
        self.assertEqual(messages, before)

    def test_missing_tool_result_is_rejected(self):
        messages = [
            _assistant_calls("call-a", "call-b"),
            _tool_result("call-a"),
            {"role": "assistant", "content": "错误地提前结束"},
        ]
        with self.assertRaisesRegex(ValueError, "缺少完整 tool 结果"):
            validate_tool_protocol(messages)

    def test_unknown_tool_call_id_is_rejected(self):
        messages = [_assistant_calls("call-a"), _tool_result("call-b")]
        with self.assertRaisesRegex(ValueError, "未在上一条 assistant 中声明"):
            validate_tool_protocol(messages)

    def test_duplicate_or_empty_call_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "重复 id"):
            validate_tool_protocol([_assistant_calls("call-a", "call-a")])
        with self.assertRaisesRegex(ValueError, "稳定 id"):
            validate_tool_protocol([_assistant_calls("")])

    def test_pending_tail_is_allowed_only_when_explicit(self):
        messages = [_assistant_calls("call-a")]
        with self.assertRaisesRegex(ValueError, "未配对工具调用"):
            validate_tool_protocol(messages)
        validate_tool_protocol(messages, allow_pending_tail=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
