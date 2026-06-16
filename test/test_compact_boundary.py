"""compact_boundary.py 单元测试。

覆盖：
1. boundary 消息构造（角色 system、kind=compact_boundary、前缀【上下文压缩】）
2. find_last_compact_boundary_index 倒序查找
3. 多 boundary 时取最后一个
4. get_messages_after_compact_boundary 切片包含 boundary 本身
5. 没有 boundary 时返回原列表
"""

from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.compact_boundary import (
    COMPACT_BOUNDARY_KIND,
    COMPACT_BOUNDARY_PREFIX,
    find_last_compact_boundary_index,
    get_messages_after_compact_boundary,
    make_compact_boundary_message,
)
from core.message import Message


def _user_text(msg: Message) -> str:
    """user 消息 content 是多模态 list,这里把 text 提取出来便于断言。"""
    if isinstance(msg.content, list):
        for item in msg.content:
            if isinstance(item, dict) and item.get("type") == "text":
                return str(item.get("text") or "")
    return str(msg.content or "")


class TestMakeCompactBoundary(unittest.TestCase):
    def test_message_role_kind_and_prefix(self):
        msg = make_compact_boundary_message("旧上下文摘要")
        role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        self.assertEqual(role, "system")
        self.assertEqual(msg.metadata.get("kind"), COMPACT_BOUNDARY_KIND)
        # 前缀自动加上
        self.assertTrue(str(msg.content).startswith(COMPACT_BOUNDARY_PREFIX))
        self.assertIn("旧上下文摘要", str(msg.content))

    def test_existing_prefix_not_doubled(self):
        msg = make_compact_boundary_message(COMPACT_BOUNDARY_PREFIX + "已带前缀")
        # 前缀只出现一次
        self.assertEqual(str(msg.content).count(COMPACT_BOUNDARY_PREFIX), 1)


class TestFindLastBoundary(unittest.TestCase):
    def test_no_boundary(self):
        msgs = [
            Message.create_user_message("hi"),
            Message.create_assistant_message("hello"),
        ]
        self.assertEqual(find_last_compact_boundary_index(msgs), -1)

    def test_single_boundary(self):
        msgs = [
            Message.create_user_message("a"),
            make_compact_boundary_message("摘要1"),
            Message.create_user_message("b"),
        ]
        self.assertEqual(find_last_compact_boundary_index(msgs), 1)

    def test_multiple_boundaries_returns_last(self):
        msgs = [
            make_compact_boundary_message("早期摘要"),
            Message.create_user_message("u"),
            make_compact_boundary_message("近期摘要"),
            Message.create_user_message("v"),
        ]
        self.assertEqual(find_last_compact_boundary_index(msgs), 2)


class TestGetMessagesAfterBoundary(unittest.TestCase):
    def test_no_boundary_returns_all(self):
        msgs = [
            Message.create_user_message("a"),
            Message.create_assistant_message("b"),
        ]
        out = get_messages_after_compact_boundary(msgs)
        self.assertEqual(len(out), 2)

    def test_returns_boundary_inclusive(self):
        boundary = make_compact_boundary_message("摘要")
        msgs = [
            Message.create_user_message("早期"),
            Message.create_assistant_message("早期回"),
            boundary,
            Message.create_user_message("新轮"),
        ]
        out = get_messages_after_compact_boundary(msgs)
        self.assertEqual(len(out), 2)
        self.assertEqual(
            (out[0].metadata or {}).get("kind"),
            COMPACT_BOUNDARY_KIND,
        )
        self.assertEqual(_user_text(out[1]), "新轮")

    def test_multiple_boundaries_uses_latest(self):
        msgs = [
            make_compact_boundary_message("早期"),
            Message.create_user_message("中间内容"),
            make_compact_boundary_message("近期"),
            Message.create_user_message("尾部"),
        ]
        out = get_messages_after_compact_boundary(msgs)
        self.assertEqual(len(out), 2)
        self.assertIn("近期", str(out[0].content))
        self.assertEqual(_user_text(out[1]), "尾部")


if __name__ == "__main__":
    unittest.main(verbosity=2)
