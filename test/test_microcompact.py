"""microcompact.py 单元测试。

覆盖:
1. tool_result 数 < 阈值时不动
2. 超阈值时把最旧的若干条 content 替换为占位
3. 占位符是合法 JSON、tool_call_id / name 保留
4. 已被清理过的消息再次扫描时跳过(幂等)
5. 阈值 = 10 / 保留最近 = 5 的边界
"""

from __future__ import annotations

import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.microcompact import (
    CLEARED_PLACEHOLDER,
    MICROCOMPACT_KEEP_RECENT,
    MICROCOMPACT_THRESHOLD,
    apply_microcompact,
)


def _make_messages(n_tool_results: int) -> list:
    """构造一组 messages:每个 tool_result 前面跟一个 assistant.tool_calls。"""
    msgs = [{"role": "user", "content": "hi"}]
    for i in range(n_tool_results):
        call_id = f"call_{i}"
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "file_read", "arguments": "{}"},
            }],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": call_id,
            "name": "file_read",
            "content": f"原始内容 {i}",
        })
    msgs.append({"role": "user", "content": "继续"})
    return msgs


class TestApplyMicrocompact(unittest.TestCase):
    def test_under_threshold_no_change(self):
        msgs = _make_messages(MICROCOMPACT_THRESHOLD - 1)
        before = json.dumps(msgs, ensure_ascii=False)
        cleared = apply_microcompact(msgs)
        self.assertEqual(cleared, 0)
        self.assertEqual(json.dumps(msgs, ensure_ascii=False), before)

    def test_at_threshold_clears_oldest(self):
        msgs = _make_messages(MICROCOMPACT_THRESHOLD)
        cleared = apply_microcompact(msgs)
        self.assertEqual(cleared, MICROCOMPACT_THRESHOLD - MICROCOMPACT_KEEP_RECENT)

        tool_msgs = [m for m in msgs if m.get("role") == "tool"]
        cleared_msgs = [m for m in tool_msgs if m["content"] == CLEARED_PLACEHOLDER]
        kept_msgs = [m for m in tool_msgs if m["content"] != CLEARED_PLACEHOLDER]
        self.assertEqual(len(cleared_msgs), cleared)
        self.assertEqual(len(kept_msgs), MICROCOMPACT_KEEP_RECENT)
        # 最旧的 N 条被清,最近 N 条保留(顺序断言)
        for idx in range(cleared):
            self.assertEqual(tool_msgs[idx]["content"], CLEARED_PLACEHOLDER)
        for idx in range(cleared, MICROCOMPACT_THRESHOLD):
            self.assertNotEqual(tool_msgs[idx]["content"], CLEARED_PLACEHOLDER)

    def test_pairing_preserved_after_clear(self):
        """清理后 assistant.tool_calls.id 与 tool_message.tool_call_id 仍然配对。"""
        msgs = _make_messages(12)
        apply_microcompact(msgs)

        ids_from_assistant = []
        for m in msgs:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    ids_from_assistant.append(tc["id"])
        ids_from_tool = [m["tool_call_id"] for m in msgs if m.get("role") == "tool"]
        self.assertEqual(ids_from_assistant, ids_from_tool)

        # 被清的 tool 消息仍然带有 name / tool_call_id
        for m in msgs:
            if m.get("role") == "tool" and m.get("content") == CLEARED_PLACEHOLDER:
                self.assertTrue(m.get("tool_call_id"))
                self.assertEqual(m.get("name"), "file_read")

    def test_idempotent_on_already_cleared(self):
        """已经清理过的不会被重新计数,也不会被再次替换。"""
        msgs = _make_messages(15)
        first = apply_microcompact(msgs)
        # 第二次跑,已经清掉的会被识别为"已处理",剩余条数等于 KEEP_RECENT
        # 还没到阈值,不应该再清。
        second = apply_microcompact(msgs)
        self.assertEqual(second, 0)
        # 总清理数仍等于第一次
        cleared_now = sum(
            1 for m in msgs
            if m.get("role") == "tool" and m.get("content") == CLEARED_PLACEHOLDER
        )
        self.assertEqual(cleared_now, first)

    def test_placeholder_is_valid_json(self):
        data = json.loads(CLEARED_PLACEHOLDER)
        self.assertIs(data["cleared"], True)
        self.assertIn("hint", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
