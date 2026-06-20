from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.event_bus import EventBus, collect_all
from agent.events import Done, SubagentProgress, ToolStart
from agent.subagents import ScopedEventBus, SubagentRegistry, SubagentTaskRegistry


class TestSubagentRegistry(unittest.TestCase):
    def test_loads_project_markdown_agent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_dir = root / ".cbagent" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "product-manager.md").write_text(
                "---\n"
                "name: product-manager\n"
                "description: Product manager reviewer\n"
                "tools: [file_read, grep]\n"
                "max_turns: 5\n"
                "---\n"
                "# PM\n\nFocus on user value and scope tradeoffs.\n",
                encoding="utf-8",
            )

            registry = SubagentRegistry(root, user_agents_dir=root / "no-user-agents")
            item = registry.get("product-manager")

            self.assertEqual(item.name, "product-manager")
            self.assertEqual(item.description, "Product manager reviewer")
            self.assertEqual(item.tools, ["file_read", "grep"])
            self.assertEqual(item.max_turns, 5)
            self.assertIn("user value", item.system_prompt)


class TestScopedEventBus(unittest.TestCase):
    def test_drops_child_done_but_forwards_progress(self) -> None:
        parent = EventBus()
        events = collect_all(parent)
        scoped = ScopedEventBus(
            parent,
            subagent_id="sub_1",
            subagent_type="general-purpose",
            description="test",
        )

        scoped.emit(ToolStart(call_id="call_1", name="file_read", arguments={}, round_idx=2))
        scoped.emit(Done(final_answer="child final", rounds_used=3))

        self.assertTrue(any(isinstance(e, SubagentProgress) for e in events))
        self.assertFalse(any(isinstance(e, Done) for e in events))
        self.assertEqual(scoped.final_answer, "child final")
        self.assertEqual(scoped.rounds_used, 3)


class TestSubagentTaskRegistry(unittest.TestCase):
    def test_background_task_writes_output_and_drains_once(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentTaskRegistry(Path(td))

            def target(task, _token):
                return {"status": "done", "content": "finished", "rounds_used": 2}

            task = registry.spawn(
                subagent_id="sub_1",
                subagent_type="general-purpose",
                description="test",
                prompt="do it",
                target=target,
            )
            registry.wait(task.id, timeout=5)

            self.assertEqual(task.status, "done")
            self.assertTrue(Path(task.output_path).exists())
            self.assertIn("finished", Path(task.output_path).read_text(encoding="utf-8"))

            first = registry.drain_notifications()
            second = registry.drain_notifications()
            self.assertEqual([t.id for t in first], [task.id])
            self.assertEqual(second, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
