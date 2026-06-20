"""agent hooks 子系统单元测试。

覆盖：
- matcher 四类匹配规则
- load_hooks_config 的容错（缺失/坏 JSON/非法结构/不支持的类型）
- HookManager.fire 的决策合并（exit code 0/2/其它、stdout JSON 解析、
  deny/updatedInput/additionalContext、多 handler 短路）

command 执行通过 mock subprocess.run 模拟，不真正起子进程，保证测试跨平台
稳定、无外部依赖。与本仓库其它测试一致用 unittest（venv 未装 pytest）。

跑法：
    ../venv/python.exe test/test_hooks.py
    ../venv/python.exe -m unittest test.test_hooks -v
"""

from __future__ import annotations

import os
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# 允许从 test/ 直接定位到包根
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.hooks import HookManager, load_hooks_config, matches  # noqa: E402
from agent.hooks import manager as hooks_manager  # noqa: E402
from agent.hooks.config import HookGroup, HookHandler  # noqa: E402
from agent.event_bus import EventBus, collect_all  # noqa: E402
from agent.events import HookCompleted, HookStarted  # noqa: E402


class _Proc:
    """subprocess.run 返回值的最小桩。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _patch_run(*, exit_code=0, stdout="", stderr=""):
    """返回一个 patch 上下文，把 subprocess.run 替换成固定结果。"""
    return mock.patch.object(
        hooks_manager.subprocess, "run",
        return_value=_Proc(exit_code, stdout, stderr),
    )


def _manager(event, matcher="*", command="x"):
    cfg = {event: [HookGroup(matcher=matcher, handlers=[HookHandler(command=command)])]}
    return HookManager(cfg, cwd=Path("."))


class TestMatcher(unittest.TestCase):
    def test_match_all(self):
        self.assertTrue(matches("*", "bash"))
        self.assertTrue(matches("", "bash"))
        self.assertTrue(matches(None, "bash"))

    def test_exact_and_list(self):
        self.assertTrue(matches("bash", "bash"))
        self.assertFalse(matches("bash", "grep"))
        self.assertTrue(matches("bash|file_edit", "file_edit"))
        self.assertFalse(matches("bash|file_edit", "grep"))

    def test_regex(self):
        self.assertTrue(matches("mcp__.*", "mcp__memory__create"))
        self.assertTrue(matches("^Note", "Notebook"))
        self.assertFalse(matches("^Note", "bash"))

    def test_invalid_regex_no_crash(self):
        self.assertFalse(matches("(unclosed", "anything"))


class TestConfigLoad(unittest.TestCase):
    def _write(self, text):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8",
        )
        tmp.write(text)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return Path(tmp.name)

    def test_missing_file(self):
        self.assertEqual(load_hooks_config(Path(tempfile.gettempdir()) / "nope_hooks.json"), {})

    def test_bad_json(self):
        self.assertEqual(load_hooks_config(self._write("{ not json")), {})

    def test_non_object_root(self):
        self.assertEqual(load_hooks_config(self._write("[1,2,3]")), {})

    def test_skips_unsupported_event_and_type(self):
        cfg = load_hooks_config(self._write(
            """
            {
              "hooks": {
                "PreToolUse": [
                  {"matcher": "bash", "hooks": [
                    {"type": "command", "command": "echo hi", "timeout": 5}
                  ]}
                ],
                "NotARealEvent": [
                  {"matcher": "*", "hooks": [{"type": "command", "command": "x"}]}
                ],
                "PostToolUse": [
                  {"matcher": "*", "hooks": [{"type": "http", "url": "http://x"}]}
                ]
              }
            }
            """
        ))
        self.assertIn("PreToolUse", cfg)
        self.assertEqual(cfg["PreToolUse"][0].handlers[0].command, "echo hi")
        self.assertEqual(cfg["PreToolUse"][0].handlers[0].timeout, 5.0)
        self.assertNotIn("NotARealEvent", cfg)
        # 不支持的 handler 类型 → 该组无合法 handler → 整组丢弃
        self.assertNotIn("PostToolUse", cfg)

    def test_subagent_events_are_supported(self):
        cfg = load_hooks_config(self._write(
            """
            {
              "hooks": {
                "SubagentStart": [
                  {"matcher": "*", "hooks": [{"type": "command", "command": "echo start"}]}
                ],
                "SubagentStop": [
                  {"matcher": "*", "hooks": [{"type": "command", "command": "echo stop"}]}
                ]
              }
            }
            """
        ))
        self.assertIn("SubagentStart", cfg)
        self.assertIn("SubagentStop", cfg)


class TestFire(unittest.TestCase):
    def test_no_config_returns_empty(self):
        out = HookManager({}, cwd=Path(".")).fire(
            "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
        )
        self.assertFalse(out.blocked)
        self.assertIsNone(out.updated_input)
        self.assertEqual(out.additional_context, "")

    def test_exit0_passes(self):
        with _patch_run(exit_code=0, stdout=""):
            out = _manager("PreToolUse").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertFalse(out.blocked)

    def test_exit2_blocks(self):
        with _patch_run(exit_code=2, stderr="危险命令被拦截"):
            out = _manager("PreToolUse").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertTrue(out.blocked)
        self.assertIn("危险命令被拦截", out.block_reason)

    def test_other_exit_non_blocking(self):
        with _patch_run(exit_code=1, stderr="warn"):
            out = _manager("PreToolUse").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertFalse(out.blocked)

    def test_json_deny(self):
        stdout = (
            '{"hookSpecificOutput": {"permissionDecision": "deny", '
            '"permissionDecisionReason": "no rm allowed"}}'
        )
        with _patch_run(exit_code=0, stdout=stdout):
            out = _manager("PreToolUse").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertTrue(out.blocked)
        self.assertEqual(out.block_reason, "no rm allowed")

    def test_json_updated_input(self):
        with _patch_run(exit_code=0, stdout='{"hookSpecificOutput": {"updatedInput": {"command": "ls -la"}}}'):
            out = _manager("PreToolUse").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertEqual(out.updated_input, {"command": "ls -la"})

    def test_json_additional_context(self):
        with _patch_run(exit_code=0, stdout='{"hookSpecificOutput": {"additionalContext": "项目使用 4 空格缩进"}}'):
            out = _manager("PostToolUse").fire(
                "PostToolUse", {"tool_name": "file_edit"}, matcher_value="file_edit",
            )
        self.assertIn("4 空格缩进", out.additional_context)

    def test_continue_false_sets_stop(self):
        with _patch_run(exit_code=0, stdout='{"continue": false, "stopReason": "stop now"}'):
            out = _manager("Stop").fire("Stop", {}, matcher_value="")
        self.assertTrue(out.stop)
        self.assertEqual(out.block_reason, "stop now")

    def test_matcher_miss_skips(self):
        with _patch_run(exit_code=2, stderr="should not run"):
            out = _manager("PreToolUse", matcher="file_edit").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertFalse(out.blocked)

    def test_timeout_non_blocking(self):
        def boom(*a, **k):
            raise hooks_manager.subprocess.TimeoutExpired(cmd="x", timeout=1)

        with mock.patch.object(hooks_manager.subprocess, "run", side_effect=boom):
            out = _manager("PreToolUse").fire(
                "PreToolUse", {"tool_name": "bash"}, matcher_value="bash",
            )
        self.assertFalse(out.blocked)

    def test_has_event_and_enabled(self):
        mgr = _manager("PreToolUse")
        self.assertTrue(mgr.enabled)
        self.assertTrue(mgr.has_event("PreToolUse"))
        self.assertFalse(mgr.has_event("Stop"))
        self.assertFalse(HookManager({}, cwd=Path(".")).enabled)

    def test_scope_fields_are_sent_to_stdin_and_events(self):
        cfg = {
            "PreToolUse": [
                HookGroup(matcher="bash", handlers=[HookHandler(command="echo ok")])
            ]
        }
        bus = EventBus()
        events = collect_all(bus)
        captured = {}

        def fake_run(*_args, **kwargs):
            captured["stdin"] = kwargs.get("input")
            return _Proc(0, "", "")

        mgr = HookManager(
            cfg,
            cwd=Path("."),
            event_bus=bus,
            session_id="root-session",
        ).with_context(
            agent_scope="subagent",
            subagent_id="sub_1",
            subagent_type="reviewer",
            parent_session_id="root-session",
            task_id="task_1",
            run_in_background=True,
        )

        with mock.patch.object(hooks_manager.subprocess, "run", side_effect=fake_run):
            mgr.fire(
                "PreToolUse",
                {"tool_name": "bash", "tool_input": {}, "tool_call_id": "call_1"},
                matcher_value="bash",
                round_idx=7,
            )

        stdin = json.loads(captured["stdin"])
        self.assertEqual(stdin["agent_scope"], "subagent")
        self.assertEqual(stdin["subagent_id"], "sub_1")
        self.assertEqual(stdin["subagent_type"], "reviewer")
        self.assertEqual(stdin["parent_session_id"], "root-session")
        self.assertEqual(stdin["task_id"], "task_1")
        self.assertTrue(stdin["run_in_background"])
        self.assertEqual(stdin["round_idx"], 7)
        self.assertEqual(stdin["tool_call_id"], "call_1")

        started = next(e for e in events if isinstance(e, HookStarted))
        completed = next(e for e in events if isinstance(e, HookCompleted))
        self.assertEqual(started.agent_scope, "subagent")
        self.assertEqual(started.subagent_id, "sub_1")
        self.assertEqual(completed.hook_call_id, started.hook_call_id)
        self.assertEqual(completed.task_id, "task_1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
