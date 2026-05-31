"""TodoTool 事件广播单测。

验证写入 todo 后会 emit 一条 TodoListUpdated；纯读取不发事件；
event_bus=None 时静默不发。
"""

from __future__ import annotations

import sys
import unittest

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent.event_bus import EventBus
from agent.events import TodoListUpdated
from tools.tools.todo_tool import TodoTool


class TestTodoToolEvents(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = []
        self.bus.subscribe(self.events.append, TodoListUpdated)
        self.tool = TodoTool(event_bus=self.bus)

    def test_write_emits_event(self):
        self.tool.run({"todos": [
            {"id": "1", "content": "task A", "status": "pending"},
            {"id": "2", "content": "task B", "status": "in_progress"},
        ]})
        self.assertEqual(len(self.events), 1)
        ev = self.events[0]
        self.assertIsInstance(ev, TodoListUpdated)
        self.assertEqual(len(ev.items), 2)
        self.assertEqual(ev.items[0]["id"], "1")
        self.assertEqual(ev.items[1]["status"], "in_progress")

    def test_read_does_not_emit(self):
        # 先写入一次（产生 1 条），然后纯读取（不应再产生）
        self.tool.run({"todos": [{"id": "1", "content": "x"}]})
        self.events.clear()
        self.tool.run({})  # 读
        self.assertEqual(self.events, [])

    def test_merge_emits_full_list(self):
        self.tool.run({"todos": [{"id": "1", "content": "A"}]})
        self.tool.run({"todos": [{"id": "2", "content": "B"}], "merge": True})
        # 两次写各发一条
        self.assertEqual(len(self.events), 2)
        # merge 后第二条 emit 的是合并后的全量列表
        self.assertEqual([i["id"] for i in self.events[1].items], ["1", "2"])

    def test_no_bus_no_crash(self):
        # event_bus=None 时静默执行（无 bus 路径）
        tool = TodoTool(event_bus=None)
        result = tool.run({"todos": [{"id": "1", "content": "x"}]})
        self.assertIn('"todos"', result)


if __name__ == "__main__":
    unittest.main()
