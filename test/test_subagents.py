from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.cancel import CancelToken
from agent.event_bus import EventBus, collect_all
from agent.events import Done, SubagentProgress, ToolComplete, ToolStart
from agent.session import AgentSession
from agent.subagents import ScopedEventBus, SubagentTaskRegistry
from agent.work_context import LocalSessionStore
from subagent.context import reset_current_parent_session_id, set_current_parent_session_id
from subagent.manager import SubagentTaskManager
from subagent.models import SubagentDefinition, SubagentPermissionPolicy
from subagent.permissions import SubagentExecutionPolicy
from subagent.registry import SubagentRegistry
from tools.toolRegistry import ToolRegistry
from tools.tools.bash_session import get_session, reset_session
from tools.tools.pending_images import (
    drain_images,
    queue_image,
    reset_pending_image_buffer,
    set_pending_image_buffer,
)
from tools.tools.local_search import (
    GlobTool,
    reset_search_ignore_dirs,
    set_search_ignore_dirs,
)
from tools.tools.subagent_tool import AgentTaskTool, AgentTool, SubagentRunner


def wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class TestSubagentRegistry(unittest.TestCase):
    def test_loads_four_builtin_agents(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRegistry(Path(td), user_agents_dir=Path(td) / "no-user-agents")
            self.assertEqual(
                [item.name for item in registry.list()],
                ["explore", "general", "reviewer", "worker"],
            )
            self.assertFalse(registry.get("explore").permissions.workspace_write)
            self.assertTrue(registry.get("worker").permissions.workspace_write)
            self.assertFalse(registry.get("worker").permissions.allow_spawn)

    def test_loads_project_markdown_agent_with_safe_yaml(self) -> None:
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
                "permissions:\n"
                "  bash_mode: deny\n"
                "---\n"
                "# PM\n\nFocus on user value and scope tradeoffs.\n",
                encoding="utf-8",
            )

            registry = SubagentRegistry(root, user_agents_dir=root / "no-user-agents")
            item = registry.get("product-manager")

            self.assertEqual(item.name, "product-manager")
            self.assertEqual(item.description, "Product manager reviewer")
            self.assertEqual(item.tools, ("file_read", "grep"))
            self.assertEqual(item.max_turns, 5)
            self.assertIn("user value", item.system_prompt)

    def test_custom_agent_without_tools_gets_minimal_readonly_set(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_dir = root / ".cbagent" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "minimal.md").write_text(
                "---\nname: minimal\n---\n只做调查。\n",
                encoding="utf-8",
            )
            item = SubagentRegistry(root, user_agents_dir=root / "none").get("minimal")
            self.assertEqual(item.tools, ("file_read", "glob", "grep", "ls"))

    def test_custom_agent_with_empty_tools_has_no_tools(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_dir = root / ".cbagent" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "prompt-only.md").write_text(
                "---\nname: prompt-only\ntools: []\n---\n只做纯文本推理。\n",
                encoding="utf-8",
            )
            item = SubagentRegistry(root, user_agents_dir=root / "none").get("prompt-only")
            self.assertEqual(item.tools, ())

    def test_unknown_agent_is_rejected_instead_of_falling_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRegistry(Path(td), user_agents_dir=Path(td) / "none")
            with self.assertRaisesRegex(ValueError, "未知 subagent_type"):
                registry.get("missing")
            # 旧名称只作为明确兼容别名，不影响未知名称的严格校验。
            self.assertEqual(registry.get("general-purpose").name, "general")
            self.assertEqual(registry.get("Explored").name, "explore")

    def test_invalid_definition_is_reported_without_breaking_registry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_dir = root / ".cbagent" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "broken.md").write_text(
                "---\npermissions: invalid\n---\nPrompt\n",
                encoding="utf-8",
            )
            registry = SubagentRegistry(root, user_agents_dir=root / "none")
            self.assertEqual(len(registry.errors()), 1)
            self.assertEqual(registry.get("explore").name, "explore")

    def test_invalid_max_turns_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            agents_dir = root / ".cbagent" / "agents"
            agents_dir.mkdir(parents=True)
            (agents_dir / "broken-turns.md").write_text(
                "---\nname: broken-turns\nmax_turns: many\n---\nPrompt\n",
                encoding="utf-8",
            )
            registry = SubagentRegistry(root, user_agents_dir=root / "none")
            self.assertEqual(len(registry.errors()), 1)
            self.assertIn("max_turns", registry.errors()[0]["error"])


class TestScopedEventBus(unittest.TestCase):
    def test_drops_child_done_but_forwards_structured_progress(self) -> None:
        parent = EventBus()
        events = collect_all(parent)
        scoped = ScopedEventBus(
            parent,
            subagent_id="sub_1",
            subagent_type="general",
            description="test",
        )

        scoped.emit(ToolStart(call_id="call_1", name="file_read", arguments={"path": "a.py"}, round_idx=2))
        scoped.emit(Done(final_answer="child final", rounds_used=3))

        progress = next(event for event in events if isinstance(event, SubagentProgress))
        self.assertEqual(progress.tool_name, "file_read")
        self.assertEqual(progress.tool_call_id, "call_1")
        self.assertFalse(any(isinstance(event, Done) for event in events))
        self.assertEqual(scoped.final_answer, "child final")
        self.assertEqual(scoped.rounds_used, 3)

    def test_managed_task_does_not_fallback_after_terminal_state(self) -> None:
        parent = EventBus()
        events = collect_all(parent)
        manager = MagicMock()
        manager.record_child_event.return_value = None
        scoped = ScopedEventBus(
            parent,
            subagent_id="sub_1",
            subagent_type="worker",
            description="test",
            task_id="task_1",
            task_manager=manager,
        )

        scoped.emit(ToolStart(
            call_id="late-call",
            name="bash",
            arguments={"command": "pwd"},
            round_idx=2,
        ))

        self.assertEqual(events, [])


class TestSubagentTaskManager(unittest.TestCase):
    def test_runs_up_to_configured_concurrency_and_queues_the_rest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=2)
            release = threading.Event()
            state_lock = threading.Lock()
            running = 0
            max_running = 0

            def target(_task, _token):
                nonlocal running, max_running
                with state_lock:
                    running += 1
                    max_running = max(max_running, running)
                release.wait(2)
                with state_lock:
                    running -= 1
                return {"status": "completed", "content": "finished", "rounds_used": 1}

            try:
                tasks = [
                    manager.spawn(
                        owner_session_id="session-a",
                        subagent_id=f"sub-{index}",
                        subagent_type="explore",
                        description="test",
                        prompt="do it",
                        target=target,
                    )
                    for index in range(3)
                ]
                self.assertTrue(wait_until(lambda: sum(task.status == "running" for task in tasks) == 2))
                self.assertEqual(sum(task.status == "queued" for task in tasks), 1)
                self.assertEqual(max_running, 2)
                release.set()
                self.assertTrue(wait_until(lambda: all(task.status == "completed" for task in tasks)))
            finally:
                release.set()
                manager.shutdown()

    def test_progress_snapshot_cursor_redaction_and_owner_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=1)
            release = threading.Event()

            def target(task, _token):
                manager.record_child_event(
                    task.id,
                    ToolStart(
                        call_id="call-1",
                        name="bash",
                        arguments={
                            "command": "curl -H 'Authorization: Bearer abc.def' example.com",
                            "api_key": "secret-value",
                        },
                        round_idx=1,
                    ),
                )
                release.wait(2)
                manager.record_child_event(
                    task.id,
                    ToolComplete(
                        call_id="call-1",
                        name="bash",
                        result="ok",
                        duration_seconds=0.1,
                        round_idx=1,
                    ),
                )
                return {"status": "completed", "content": "done"}

            try:
                task = manager.spawn(
                    owner_session_id="session-a",
                    subagent_id="sub-1",
                    subagent_type="worker",
                    description="test",
                    prompt="do it",
                    target=target,
                )
                self.assertTrue(wait_until(lambda: task.current_tool_name == "bash"))
                first = manager.inspect(task.id, owner_session_id="session-a", cursor=0)
                self.assertIsNotNone(first)
                self.assertEqual(first["task"]["current_tool"]["arguments"]["api_key"], "[已脱敏]")
                self.assertNotIn("abc.def", first["task"]["current_tool"]["arguments"]["command"])
                self.assertIsNone(manager.inspect(task.id, owner_session_id="session-b", cursor=0))
                cursor = first["next_cursor"]
                release.set()
                self.assertTrue(wait_until(lambda: task.status == "completed"))
                second = manager.inspect(task.id, owner_session_id="session-a", cursor=cursor)
                self.assertTrue(any(event["type"] == "completed" for event in second["events"]))
                self.assertTrue(Path(task.output_path).exists())
                self.assertEqual(Path(task.output_path).read_text(encoding="utf-8"), "done")
            finally:
                release.set()
                manager.shutdown()

    def test_parallel_tools_remain_waiting_until_all_calls_finish(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=1)
            observations = []

            def target(task, _token):
                manager.record_child_event(task.id, ToolStart(
                    call_id="call-a",
                    name="file_read",
                    arguments={"path": "a.py"},
                    round_idx=1,
                ))
                manager.record_child_event(task.id, ToolStart(
                    call_id="call-b",
                    name="grep",
                    arguments={"pattern": "TODO"},
                    round_idx=1,
                ))
                manager.record_child_event(task.id, ToolComplete(
                    call_id="call-a",
                    name="file_read",
                    result="ok",
                    duration_seconds=0.1,
                    round_idx=1,
                ))
                observations.append(task.to_dict())
                manager.record_child_event(task.id, ToolComplete(
                    call_id="call-b",
                    name="grep",
                    result="ok",
                    duration_seconds=0.2,
                    round_idx=1,
                ))
                observations.append(task.to_dict())
                return {"status": "completed", "content": "done"}

            try:
                task, _result = manager.run_foreground(
                    owner_session_id="session-a",
                    subagent_id="sub-tools",
                    subagent_type="explore",
                    description="parallel tools",
                    prompt="parallel tools",
                    target=target,
                    cancel_token=CancelToken(),
                )
                self.assertEqual(observations[0]["status"], "waiting_tool")
                self.assertEqual(observations[0]["active_tool_count"], 1)
                self.assertEqual(observations[0]["current_tool"]["name"], "grep")
                self.assertEqual(observations[1]["status"], "running")
                self.assertEqual(observations[1]["active_tool_count"], 0)
                self.assertIsNone(observations[1]["current_tool"])
                self.assertEqual(task.status, "completed")
            finally:
                manager.shutdown()

    def test_parent_updates_are_incremental_and_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=1)
            try:
                task = manager.spawn(
                    owner_session_id="session-a",
                    subagent_id="sub-1",
                    subagent_type="explore",
                    description="inspect",
                    prompt="do it",
                    target=lambda _task, _token: {"status": "completed", "content": "done"},
                )
                self.assertTrue(wait_until(lambda: task.status == "completed"))
                self.assertEqual(manager.drain_parent_updates("session-b"), "")
                first = manager.drain_parent_updates("session-a")
                second = manager.drain_parent_updates("session-a")
                self.assertIn(task.id, first)
                self.assertIn('result_preview: "done"', first)
                self.assertEqual(second, "")
            finally:
                manager.shutdown()

    def test_message_mailbox_and_queued_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=1)
            release = threading.Event()
            second_ran = threading.Event()
            terminal_ids = []
            manager.subscribe_events(
                lambda task, event: terminal_ids.append(task.id)
                if event["type"] in {"completed", "failed", "cancelled", "orphaned"}
                else None
            )
            try:
                first = manager.spawn(
                    owner_session_id="session-a",
                    subagent_id="sub-1",
                    subagent_type="worker",
                    description="first",
                    prompt="first",
                    target=lambda _task, _token: (release.wait(2) or {"status": "completed"}),
                )
                second = manager.spawn(
                    owner_session_id="session-a",
                    subagent_id="sub-2",
                    subagent_type="worker",
                    description="second",
                    prompt="second",
                    target=lambda _task, _token: (
                        second_ran.set() or {"status": "completed"}
                    ),
                )
                self.assertTrue(wait_until(lambda: first.status == "running" and second.status == "queued"))
                manager.send_message(first.id, owner_session_id="session-a", message="补充检查测试")
                self.assertEqual(manager.drain_messages(first.id), ["补充检查测试"])
                manager.cancel(second.id, owner_session_id="session-a")
                self.assertEqual(second.status, "cancelled")
                self.assertIsNotNone(second.finished_at)
                release.set()
                self.assertTrue(wait_until(lambda: first.status == "completed"))
                self.assertTrue(wait_until(lambda: manager._queue.unfinished_tasks == 0))
                self.assertFalse(second_ran.is_set())
                self.assertIn(second.id, terminal_ids)
            finally:
                release.set()
                manager.shutdown()

    def test_shutdown_orphaned_state_cannot_be_rewritten_by_late_worker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=1)
            release = threading.Event()
            queued_ran = threading.Event()

            running = manager.spawn(
                owner_session_id="session-a",
                subagent_id="sub-running",
                subagent_type="worker",
                description="running",
                prompt="running",
                target=lambda _task, _token: (
                    release.wait(2) or {"status": "completed", "content": "late"}
                ),
            )
            queued = manager.spawn(
                owner_session_id="session-a",
                subagent_id="sub-queued",
                subagent_type="worker",
                description="queued",
                prompt="queued",
                target=lambda _task, _token: (
                    queued_ran.set() or {"status": "completed"}
                ),
            )
            self.assertTrue(wait_until(lambda: running.status == "running" and queued.status == "queued"))

            manager.shutdown(timeout=0)
            self.assertEqual(running.status, "orphaned")
            self.assertEqual(queued.status, "cancelled")
            release.set()
            self.assertTrue(wait_until(lambda: manager._queue.unfinished_tasks == 0))
            self.assertEqual(running.status, "orphaned")
            self.assertEqual(queued.status, "cancelled")
            self.assertFalse(queued_ran.is_set())

    def test_exception_after_cancel_keeps_cancelled_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentTaskManager(Path(td), max_workers=1)
            entered = threading.Event()

            def target(_task, token):
                entered.set()
                self.assertTrue(wait_until(token.is_cancelled))
                raise RuntimeError("cancelled operation stopped")

            try:
                task = manager.spawn(
                    owner_session_id="session-a",
                    subagent_id="sub-cancel-error",
                    subagent_type="worker",
                    description="cancel",
                    prompt="cancel",
                    target=target,
                )
                self.assertTrue(entered.wait(timeout=1.0))
                manager.cancel(task.id, owner_session_id="session-a")
                self.assertTrue(wait_until(lambda: task.status == "cancelled"))
                self.assertIn("cancelled operation stopped", task.error)
            finally:
                manager.shutdown()

    def test_restart_marks_running_snapshot_as_orphaned(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "subagent_deadbeef.json"
            snapshot.write_text(
                json.dumps({
                    "id": "subagent_deadbeef",
                    "subagent_id": "sub-1",
                    "subagent_type": "worker",
                    "owner_session_id": "session-a",
                    "description": "old",
                    "prompt": "old",
                    "status": "running",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            manager = SubagentTaskManager(root, max_workers=1)
            try:
                task = manager.get("subagent_deadbeef", "session-a")
                self.assertIsNotNone(task)
                self.assertEqual(task.status, "orphaned")
                self.assertIn("无法安全恢复", task.error)
                self.assertEqual(
                    Path(task.output_path).read_text(encoding="utf-8"),
                    task.error,
                )
            finally:
                manager.shutdown()

    def test_snapshot_cannot_redirect_runtime_files_outside_task_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            task_dir = root / "tasks"
            task_dir.mkdir()
            outside = root / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            snapshot = task_dir / "subagent_safe.json"
            snapshot.write_text(
                json.dumps({
                    "id": "../../forged",
                    "subagent_id": "sub-1",
                    "subagent_type": "worker",
                    "owner_session_id": "session-a",
                    "description": "old",
                    "prompt": "old",
                    "status": "running",
                    "output_path": str(outside),
                    "events_path": str(outside),
                    "started_at": "2026-01-01T00:00:00+00:00",
                }),
                encoding="utf-8",
            )

            manager = SubagentTaskManager(task_dir, max_workers=1)
            try:
                task = manager.get("subagent_safe", "session-a")
                self.assertIsNotNone(task)
                self.assertEqual(task.id, "subagent_safe")
                self.assertEqual(Path(task.output_path).parent, task_dir.resolve())
                self.assertEqual(Path(task.events_path).parent, task_dir.resolve())
                self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            finally:
                manager.shutdown()

    def test_restart_recovers_event_sequence_ahead_of_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "subagent_recover.json"
            snapshot.write_text(
                json.dumps({
                    "id": "subagent_recover",
                    "subagent_id": "sub-1",
                    "subagent_type": "worker",
                    "owner_session_id": "session-a",
                    "description": "recover",
                    "prompt": "recover",
                    "status": "running",
                    "event_seq": 1,
                    "recent_events": [{"seq": 1, "type": "started"}],
                    "started_at": "2026-01-01T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            snapshot.with_suffix(".events.jsonl").write_text(
                json.dumps({"seq": 2, "type": "tool_started", "message": "ahead"}) + "\n",
                encoding="utf-8",
            )

            manager = SubagentTaskManager(root, max_workers=1)
            try:
                task = manager.get("subagent_recover", "session-a")
                self.assertIsNotNone(task)
                self.assertEqual(task.status, "orphaned")
                self.assertEqual(task.event_seq, 3)
                self.assertEqual([event["seq"] for event in task.recent_events], [1, 2, 3])
            finally:
                manager.shutdown()

    def test_restart_quarantines_unknown_snapshot_status(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "subagent_unknown.json").write_text(
                json.dumps({
                    "id": "subagent_unknown",
                    "subagent_id": "sub-1",
                    "subagent_type": "worker",
                    "owner_session_id": "session-a",
                    "description": "unknown",
                    "prompt": "unknown",
                    "status": "mystery",
                    "started_at": "2026-01-01T00:00:00+00:00",
                }),
                encoding="utf-8",
            )

            manager = SubagentTaskManager(root, max_workers=1)
            try:
                task = manager.get("subagent_unknown", "session-a")
                self.assertEqual(task.status, "orphaned")
                self.assertIn("未知任务状态", task.error)
            finally:
                manager.shutdown()

    def test_legacy_done_snapshot_is_normalized_and_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            snapshot = root / "subagent_legacy.json"
            snapshot.write_text(
                json.dumps({
                    "id": "subagent_legacy",
                    "subagent_id": "sub-old",
                    "subagent_type": "general-purpose",
                    "description": "old",
                    "prompt": "old",
                    "status": "done",
                    "result": "legacy result",
                    "output_path": str(snapshot),
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "finished_at": "2026-01-01T00:00:01+00:00",
                }),
                encoding="utf-8",
            )
            manager = SubagentTaskManager(root, max_workers=1)
            try:
                task = manager.get("subagent_legacy")
                self.assertEqual(task.status, "completed")
                self.assertEqual(task.owner_session_id, "legacy-main")
                self.assertEqual(manager.adopt_legacy_tasks("session-a"), 1)
                self.assertIsNotNone(manager.get("subagent_legacy", "session-a"))
                self.assertTrue(task.output_path.endswith(".result.txt"))
            finally:
                manager.shutdown()


class TestSubagentPermissions(unittest.TestCase):
    def test_readonly_and_worker_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            registry = SubagentRegistry(workspace, user_agents_dir=workspace / "none")
            explore = SubagentExecutionPolicy(registry.get("explore"), workspace)
            worker = SubagentExecutionPolicy(registry.get("worker"), workspace)

            self.assertFalse(explore.check("file_write", {"file_path": "a.py"})[0])
            self.assertTrue(explore.check("bash", {"command": "git status"})[0])
            self.assertFalse(explore.check("bash", {"command": "rm -f a.py"})[0])
            self.assertFalse(explore.check("file_read", {"file_path": "/etc/passwd"})[0])
            self.assertFalse(explore.check("file_read", {"file_path": ".cbagent/sessions/index.json"})[0])
            self.assertFalse(explore.check("file_read", {"file_path": ".env"})[0])
            self.assertTrue(explore.check("file_read", {"file_path": ".env.example"})[0])
            self.assertTrue(worker.check("file_write", {"file_path": "src/a.py"})[0])
            self.assertFalse(worker.check("file_write", {"file_path": "../outside.py"})[0])
            self.assertTrue(worker.check("bash", {"command": "pytest -q"})[0])
            self.assertFalse(worker.check("bash", {"command": "cat /etc/passwd"})[0])
            self.assertFalse(worker.check("bash", {"command": "ls .."})[0])
            self.assertFalse(worker.check("bash", {"command": r"cat C:\temp\secret.txt"})[0])
            self.assertFalse(worker.check("bash", {"command": "echo x>/tmp/out.txt"})[0])
            self.assertFalse(worker.check("bash", {"command": "tool --output=/tmp/out.txt"})[0])
            self.assertFalse(worker.check("bash", {"command": "cat $(printf /etc/passwd)"})[0])
            self.assertFalse(worker.check("bash", {"command": "python <<'PY'\nprint('x')\nPY"})[0])
            self.assertFalse(worker.check("bash", {"command": "pytest -q", "run_in_background": True})[0])
            self.assertFalse(worker.check("bash", {"command": "pytest -q &"})[0])
            self.assertTrue(worker.check("bash", {"command": "echo x > tmp/out.txt"})[0])
            self.assertTrue(worker.check("bash", {"command": "pytest -q 2>&1"})[0])
            self.assertFalse(worker.check("bash", {"command": "cat .cbagent/subagents/task.json"})[0])
            self.assertFalse(worker.check("bash", {"command": "cat .env"})[0])
            self.assertFalse(worker.check("bash", {"command": "cat .*"})[0])
            self.assertFalse(worker.check("bash", {"command": "cat $HOME"})[0])
            self.assertFalse(worker.check("bash", {"command": "find . -name '*.py'"})[0])
            self.assertFalse(worker.check("bash", {"command": "rg --hidden secret ."})[0])
            self.assertFalse(worker.check("agent", {"description": "nested"})[0])

            own_runtime = workspace / ".cbagent" / "subagent_tool_results" / "task-a"
            scoped_worker = SubagentExecutionPolicy(
                registry.get("worker"),
                workspace,
                allowed_internal_paths=(own_runtime,),
            )
            self.assertTrue(scoped_worker.check(
                "file_read",
                {"file_path": str(own_runtime / "tool_results" / "large.txt")},
            )[0])
            self.assertFalse(scoped_worker.check(
                "file_read",
                {"file_path": str(own_runtime.parent / "task-b" / "large.txt")},
            )[0])

            readonly_inherit = SubagentExecutionPolicy(
                SubagentDefinition(
                    name="readonly-inherit",
                    description="test",
                    system_prompt="test",
                    tools=("bash",),
                    permissions=SubagentPermissionPolicy(
                        bash_mode="inherit",
                        workspace_write=False,
                    ),
                ),
                workspace,
            )
            self.assertTrue(readonly_inherit.check("bash", {"command": "git status"})[0])
            self.assertFalse(readonly_inherit.check("bash", {"command": "touch denied.txt"})[0])


class TestSubagentTools(unittest.TestCase):
    def test_agent_tool_rejects_whitespace_only_task(self) -> None:
        runner = MagicMock()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = SubagentTaskManager(root / "tasks", max_workers=1)
            try:
                tool = AgentTool(
                    registry=SubagentRegistry(root, user_agents_dir=root / "none"),
                    task_manager=manager,
                    runner=runner,
                )
                result = json.loads(tool.run({"description": "   ", "prompt": "task"}))
                self.assertIn("必填", result["error"])
                runner.run.assert_not_called()
            finally:
                manager.shutdown()

    def test_agent_tool_uses_runtime_owner_and_unknown_type_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = SubagentTaskManager(root / "tasks", max_workers=1)
            registry = SubagentRegistry(root, user_agents_dir=root / "none")
            runner = MagicMock()
            runner.run.side_effect = lambda **kwargs: {
                "status": "completed",
                "content": "ok",
                "rounds_used": 1,
                "task_id": kwargs["task"].id,
            }
            tool = AgentTool(registry=registry, task_manager=manager, runner=runner)
            token = set_current_parent_session_id("session-a")
            try:
                started = json.loads(tool.run({
                    "description": "inspect",
                    "prompt": "inspect code",
                    "subagent_type": "explore",
                }))
                self.assertEqual(started["status"], "background_started")
                task = manager.get(started["task_id"], "session-a")
                self.assertIsNotNone(task)
                self.assertTrue(wait_until(lambda: task.status == "completed"))
                failed = json.loads(tool.run({
                    "description": "bad",
                    "prompt": "bad",
                    "subagent_type": "missing",
                }))
                self.assertEqual(failed["status"], "failed")
                self.assertIn("未知 subagent_type", failed["error"])
            finally:
                reset_current_parent_session_id(token)
                manager.shutdown()

    def test_agent_task_tool_enforces_owner_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = SubagentTaskManager(root / "tasks", max_workers=1)
            registry = SubagentRegistry(root, user_agents_dir=root / "none")
            task = manager.spawn(
                owner_session_id="session-a",
                subagent_id="sub-1",
                subagent_type="explore",
                description="inspect",
                prompt="inspect",
                target=lambda _task, _token: {"status": "completed", "content": "ok"},
            )
            tool = AgentTaskTool(registry=registry, task_manager=manager)
            token = set_current_parent_session_id("session-b")
            try:
                listed = json.loads(tool.run({"action": "list"}))
                output = json.loads(tool.run({"action": "output", "task_id": task.id}))
                self.assertEqual(listed["tasks"], [])
                self.assertIn("不属于当前会话", output["error"])
            finally:
                reset_current_parent_session_id(token)
                manager.shutdown()

    def test_agent_task_tool_rejects_malformed_action_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            manager = SubagentTaskManager(root / "tasks", max_workers=1)
            try:
                tool = AgentTaskTool(
                    registry=SubagentRegistry(root, user_agents_dir=root / "none"),
                    task_manager=manager,
                )
                result = json.loads(tool.run({"action": ["list"]}))
                self.assertIn("参数无效", result["error"])
            finally:
                manager.shutdown()


class TestAgentSessionRuntimeUpdates(unittest.TestCase):
    def test_subagent_does_not_inherit_parent_plan_mode(self) -> None:
        session = AgentSession(
            llm=MagicMock(),
            registry=MagicMock(),
            executor=MagicMock(),
            event_bus=EventBus(),
            is_subagent=True,
        )
        session.plan_store = MagicMock()
        session.plan_store.load.return_value = {"mode": "plan"}

        self.assertEqual(session.collaboration_mode(), "execute")
        self.assertEqual(session._plan_context_text(), "")
        session.plan_store.load.assert_not_called()
        session.plan_store.context_text.assert_not_called()

    def test_local_search_extra_ignores_are_context_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            (workspace / "src").mkdir()
            (workspace / "src" / "visible.txt").write_text("visible", encoding="utf-8")
            (workspace / ".cbagent").mkdir()
            (workspace / ".cbagent" / "private.txt").write_text("private", encoding="utf-8")
            reset_session(str(workspace))
            self.addCleanup(reset_session, _ROOT)
            tool = GlobTool()

            token = set_search_ignore_dirs({".cbagent"})
            try:
                scoped = json.loads(tool.run({"pattern": "**/*.txt"}))
            finally:
                reset_search_ignore_dirs(token)
            unscoped = json.loads(tool.run({"pattern": "**/*.txt"}))

            self.assertEqual(scoped["files"], ["src/visible.txt"])
            self.assertIn(".cbagent/private.txt", unscoped["files"])

    def test_pending_images_are_isolated_between_nested_agent_contexts(self) -> None:
        outer_token = set_pending_image_buffer()
        try:
            queue_image(call_id="outer", image_part={"type": "image_url"}, file_name="outer.png")
            inner_token = set_pending_image_buffer()
            try:
                queue_image(call_id="inner", image_part={"type": "image_url"}, file_name="inner.png")
                self.assertEqual([item["call_id"] for item in drain_images()], ["inner"])
            finally:
                reset_pending_image_buffer(inner_token)
            self.assertEqual([item["call_id"] for item in drain_images()], ["outer"])
        finally:
            reset_pending_image_buffer(outer_token)

    def test_injects_parent_progress_and_child_mailbox_before_think(self) -> None:
        manager = MagicMock()
        manager.drain_parent_updates.return_value = "progress"
        session = AgentSession(
            llm=MagicMock(),
            registry=MagicMock(),
            executor=MagicMock(),
            event_bus=EventBus(),
            subagent_task_registry=manager,
            runtime_session_id="session-a",
            runtime_message_provider=lambda: ["补充要求"],
        )
        session._inject_runtime_history("turn-a")
        self.assertEqual(len(session.history), 1)
        self.assertIn("progress", str(session.history[0].content))
        self.assertIn("补充要求", str(session.history[0].content))
        self.assertEqual(
            (session.history[0].metadata or {}).get("kind"),
            "context_evidence",
        )
        manager.drain_parent_updates.assert_called_once_with("session-a")

    def test_runtime_session_id_is_recreated_after_clear(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / "sessions")
            session = AgentSession(
                llm=MagicMock(),
                registry=MagicMock(),
                executor=MagicMock(),
                event_bus=EventBus(),
                session_store=store,
            )
            old_id = session.current_runtime_session_id()
            store.clear_active_session()
            new_id = session.current_runtime_session_id()
            self.assertNotEqual(old_id, new_id)
            self.assertTrue(new_id.startswith("session_"))

    def test_session_payload_restores_only_owned_subagent_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = LocalSessionStore(root / "sessions")
            manager = SubagentTaskManager(root / "tasks", max_workers=1)
            owner = store.active_session_id
            try:
                owned = manager.spawn(
                    owner_session_id=owner,
                    subagent_id="sub-owned",
                    subagent_type="explore",
                    description="owned",
                    prompt="owned",
                    target=lambda _task, _token: {"status": "completed", "content": "ok"},
                )
                manager.spawn(
                    owner_session_id="other-session",
                    subagent_id="sub-other",
                    subagent_type="explore",
                    description="other",
                    prompt="other",
                    target=lambda _task, _token: {"status": "completed", "content": "other"},
                )
                self.assertTrue(wait_until(lambda: owned.status == "completed"))
                session = AgentSession(
                    llm=MagicMock(),
                    registry=MagicMock(),
                    executor=MagicMock(),
                    event_bus=EventBus(),
                    session_store=store,
                    subagent_task_registry=manager,
                )
                payload = session.current_session_payload()
                self.assertEqual(
                    [task["subagent_id"] for task in payload["subagent_tasks"]],
                    ["sub-owned"],
                )
            finally:
                manager.shutdown()


class TestSubagentRunnerIntegration(unittest.TestCase):
    def test_runner_creates_isolated_session_and_finishes_task(self) -> None:
        class LegacyRegistry(ToolRegistry):
            """模拟尚未接收 Bash 隔离扩展参数的第三方注册表。"""

            def clone_filtered(
                self,
                *,
                allow_names=None,
                deny_names=None,
                event_bus=None,
            ):
                return super().clone_filtered(
                    allow_names=allow_names,
                    deny_names=deny_names,
                    event_bus=event_bus,
                )

        class FakeLLM:
            model = "deepseek-chat"
            is_Function_Calling = True

            def __init__(self) -> None:
                self.seen_cwd = ""

            def think(self, _messages, **_kwargs):
                self.seen_cwd = get_session().cwd
                return {
                    "answer": "child done",
                    "tool_calls": [],
                    "reasoning_content": None,
                }

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            workspace = root / "workspace"
            outside.mkdir()
            workspace.mkdir()
            reset_session(str(outside))
            self.addCleanup(reset_session, _ROOT)
            manager = SubagentTaskManager(root / "tasks", max_workers=1)
            definitions = SubagentRegistry(workspace, user_agents_dir=root / "none")
            parent_bus = EventBus()
            events = collect_all(parent_bus)
            llm = FakeLLM()
            child_message_logger = MagicMock()
            runner = SubagentRunner(
                llm=llm,
                parent_registry=LegacyRegistry(),
                parent_event_bus=parent_bus,
                task_manager=manager,
                cwd=workspace,
                ctx_enabled=False,
                message_logger_factory=lambda _scope: child_message_logger,
            )

            def target(task, token):
                return runner.run(
                    task=task,
                    definition=definitions.get("explore"),
                    description="inspect",
                    prompt="inspect code",
                    cancel_token=token,
                )

            try:
                task, result = manager.run_foreground(
                    owner_session_id="session-a",
                    subagent_id="sub-1",
                    subagent_type="explore",
                    description="inspect",
                    prompt="inspect code",
                    target=target,
                    cancel_token=CancelToken(),
                )
                self.assertEqual(task.status, "completed")
                self.assertEqual(result["content"], "child done")
                self.assertEqual(task.rounds_used, 1)
                self.assertEqual(llm.seen_cwd, str(workspace.resolve()))
                self.assertEqual(get_session().cwd, str(outside.resolve()))
                child_message_logger.close.assert_called_once_with()
                self.assertTrue(any(getattr(event, "type", "") == "subagent_started" for event in events))
                self.assertEqual(
                    sum(getattr(event, "type", "") == "subagent_completed" for event in events),
                    1,
                )
            finally:
                manager.shutdown()


class TestLegacySubagentRegistry(unittest.TestCase):
    def test_old_registry_name_keeps_wait_and_notification_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentTaskRegistry(Path(td))
            try:
                task = registry.spawn(
                    subagent_id="sub-1",
                    subagent_type="general",
                    description="test",
                    prompt="test",
                    target=lambda _task, _token: {"status": "completed", "content": "ok"},
                )
                registry.wait(task.id, timeout=2)
                self.assertEqual([item.id for item in registry.drain_notifications()], [task.id])
                self.assertEqual(registry.drain_notifications(), [])
                self.assertEqual(registry.kill(task.id).id, task.id)
            finally:
                registry.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
