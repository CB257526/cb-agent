"""message_protocol.py 单元测试。

覆盖:
1. dict 版:开头孤儿 tool 被丢弃,合法配对保留
2. dict 版:中间配对断裂的 tool 也被丢弃
3. dict 版:无 tool 消息时不动;无孤儿时不动(返回 0)
4. dict 版:原地修改保持 list 引用
5. Message 版:开头孤儿被丢弃,返回新列表不改入参
6. Message 版:boundary(system) / user / assistant 正常保留
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.message_protocol import (
    drop_orphan_tool_messages,
    drop_orphan_tool_message_objects,
)
from core.message import Message


def _assistant_call(call_id: str, name: str = "file_read") -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }],
    }


def _tool_result(call_id: str, name: str = "file_read", content: str = "x") -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
    }


class TestDropOrphanDict(unittest.TestCase):
    def test_leading_orphan_dropped(self):
        # 模拟 window 截断后切片正好从 tool(call_0) 开始:它的 assistant 父消息
        # 被切掉了,是孤儿。
        msgs = [
            _tool_result("call_0"),                 # 孤儿:无前置 assistant
            _assistant_call("call_1"),
            _tool_result("call_1"),
            {"role": "user", "content": "继续"},
        ]
        dropped = drop_orphan_tool_messages(msgs)
        self.assertEqual(dropped, 1)
        roles = [m["role"] for m in msgs]
        self.assertEqual(roles, ["assistant", "tool", "user"])
        # 保留下来的 tool 是 call_1
        self.assertEqual(msgs[1]["tool_call_id"], "call_1")

    def test_valid_pairs_untouched(self):
        msgs = [
            {"role": "user", "content": "hi"},
            _assistant_call("call_0"),
            _tool_result("call_0"),
            _assistant_call("call_1"),
            _tool_result("call_1"),
        ]
        before = list(msgs)
        dropped = drop_orphan_tool_messages(msgs)
        self.assertEqual(dropped, 0)
        self.assertEqual(msgs, before)

    def test_mid_sequence_orphan_dropped(self):
        # 中间出现一条 tool_call_id 在前文没有声明过的 tool。
        msgs = [
            _assistant_call("call_0"),
            _tool_result("call_0"),
            _tool_result("call_unknown"),           # 孤儿
            _assistant_call("call_1"),
            _tool_result("call_1"),
        ]
        dropped = drop_orphan_tool_messages(msgs)
        self.assertEqual(dropped, 1)
        tool_ids = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
        self.assertEqual(tool_ids, ["call_0", "call_1"])

    def test_no_tool_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        before = list(msgs)
        dropped = drop_orphan_tool_messages(msgs)
        self.assertEqual(dropped, 0)
        self.assertEqual(msgs, before)

    def test_inplace_keeps_reference(self):
        msgs = [_tool_result("orphan"), {"role": "user", "content": "hi"}]
        ref = msgs
        drop_orphan_tool_messages(msgs)
        self.assertIs(ref, msgs)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")

    def test_assistant_multi_tool_calls(self):
        # 一条 assistant 声明多个 tool_calls,对应多条 tool 都应保留。
        msgs = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "g", "arguments": "{}"}},
                ],
            },
            _tool_result("a", name="f"),
            _tool_result("b", name="g"),
        ]
        dropped = drop_orphan_tool_messages(msgs)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(msgs), 3)


class TestDropOrphanMessageObjects(unittest.TestCase):
    def test_leading_orphan_dropped_returns_new_list(self):
        msgs = [
            Message.create_tool_message("call_0", "file_read", "孤儿结果"),
            Message.create_assistant_message(tool_calls=[{
                "id": "call_1", "type": "function",
                "function": {"name": "file_read", "arguments": "{}"},
            }]),
            Message.create_tool_message("call_1", "file_read", "合法结果"),
        ]
        out = drop_orphan_tool_message_objects(msgs)
        # 入参不被修改
        self.assertEqual(len(msgs), 3)
        # 输出丢掉了孤儿
        self.assertEqual(len(out), 2)
        roles = [m.role.value if hasattr(m.role, "value") else str(m.role) for m in out]
        self.assertEqual(roles, ["assistant", "tool"])
        self.assertEqual(out[1].tool_call_id, "call_1")

    def test_boundary_user_assistant_preserved(self):
        boundary = Message.create_system_message("【上下文压缩】摘要")
        boundary.metadata = {"kind": "compact_boundary"}
        msgs = [
            boundary,
            Message.create_user_message("问题"),
            Message.create_assistant_message("回答"),
        ]
        out = drop_orphan_tool_message_objects(msgs)
        self.assertEqual(len(out), 3)

    def test_valid_pair_preserved(self):
        msgs = [
            Message.create_assistant_message(tool_calls=[{
                "id": "c", "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }]),
            Message.create_tool_message("c", "bash", "ok"),
        ]
        out = drop_orphan_tool_message_objects(msgs)
        self.assertEqual(len(out), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
