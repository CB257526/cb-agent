"""ToolExecutor / CancelToken 单测。

验：
- should_parallelize 判定矩阵
- 串行执行结果保序
- 并行执行结果保序、真并发（耗时检查）
- 工具异常被吞 + ToolComplete(is_error=True)
- ContextVars 跨线程传播（CancelToken 在 worker thread 可见）
- 事件 emit 完整且 round_idx 透传
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent.cancel import (
    CancelToken,
    get_current_cancel_token,
    set_current_cancel_token,
    reset_current_cancel_token,
)
from agent.event_bus import EventBus, collect_all
from agent.events import ToolComplete, ToolStart
from agent.executor import (
    READ_ONLY_IF_ACTION, READ_ONLY_TOOLS,
    ToolCallResult, ToolExecutor, should_parallelize,
)
from agent.plan_policy import PlanExecutionPolicy
from agent.platforms.context import (
    reset_current_platform_conversation,
    reset_current_platform_sender,
    set_current_platform_conversation,
    set_current_platform_sender,
)
from agent.platforms.messages import ConversationKey


def _tc(name: str, args_json: str = "{}", call_id: str = "") -> dict:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": args_json},
    }


# ========== should_parallelize ==========


class TestShouldParallelize(unittest.TestCase):
    def test_single_call_serial(self):
        self.assertFalse(should_parallelize([_tc("file_read")]))

    def test_empty_serial(self):
        self.assertFalse(should_parallelize([]))

    def test_all_read_only_parallel(self):
        self.assertTrue(should_parallelize([
            _tc("file_read"), _tc("glob"), _tc("grep"), _tc("ls"),
            _tc("search"), _tc("memory_search"),
        ]))

    def test_any_write_serial(self):
        # bash 不在白名单 → 整批串行
        self.assertFalse(should_parallelize([
            _tc("file_read"), _tc("bash"),
        ]))

    def test_action_based_read_parallel(self):
        # bash_task list/output 都是读
        self.assertTrue(should_parallelize([
            _tc("bash_task", '{"action":"list"}'),
            _tc("bash_task", '{"action":"output","task_id":"t1"}'),
        ]))

    def test_action_based_kill_serial(self):
        self.assertFalse(should_parallelize([
            _tc("bash_task", '{"action":"list"}'),
            _tc("bash_task", '{"action":"kill","task_id":"t1"}'),
        ]))

    def test_invalid_json_treated_as_write(self):
        # arguments 解析失败 → 保守串行
        self.assertFalse(should_parallelize([
            _tc("file_read", "not-json"), _tc("search"),
        ]))

    def test_unknown_tool_serial(self):
        # MCP / 未知工具 → 串行
        self.assertFalse(should_parallelize([
            _tc("mcp_some_external"), _tc("file_read"),
        ]))

    def test_all_known_tools_in_set(self):
        """sanity check：white list 跟实际工具名一致。"""
        self.assertIn("file_read", READ_ONLY_TOOLS)
        self.assertIn("glob", READ_ONLY_TOOLS)
        self.assertIn("grep", READ_ONLY_TOOLS)
        self.assertIn("ls", READ_ONLY_TOOLS)
        self.assertIn("search", READ_ONLY_TOOLS)
        self.assertIn("bash_task", READ_ONLY_IF_ACTION)
        self.assertIn("memory", READ_ONLY_IF_ACTION)


# ========== ToolExecutor 串行 ==========


class TestExecutorSerial(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = collect_all(self.bus)

    def test_serial_returns_in_order(self):
        order = []

        def runner(name, args):
            order.append(name)
            return f'{{"ran":"{name}"}}'

        ex = ToolExecutor(runner, self.bus)
        # 含 bash → 串行
        results = ex.execute([_tc("file_read"), _tc("bash"), _tc("search")], round_idx=1)
        self.assertEqual([r.name for r in results], ["file_read", "bash", "search"])
        self.assertEqual(order, ["file_read", "bash", "search"])

    def test_emits_start_and_complete(self):
        def runner(name, args):
            return "{}"
        ex = ToolExecutor(runner, self.bus)
        ex.execute([_tc("bash")], round_idx=2)
        starts = [e for e in self.events if isinstance(e, ToolStart)]
        completes = [e for e in self.events if isinstance(e, ToolComplete)]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(completes), 1)
        self.assertEqual(starts[0].name, "bash")
        self.assertEqual(starts[0].round_idx, 2)
        self.assertEqual(completes[0].is_error, False)
        self.assertGreaterEqual(completes[0].duration_seconds, 0.0)

    def test_runner_exception_caught(self):
        def runner(name, args):
            raise RuntimeError("boom")
        ex = ToolExecutor(runner, self.bus)
        results = ex.execute([_tc("bash")], round_idx=1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_error)
        self.assertIn("RuntimeError", results[0].result)

        completes = [e for e in self.events if isinstance(e, ToolComplete)]
        self.assertTrue(completes[0].is_error)

    def test_plan_policy_denial_emits_protocol_events_without_runner(self):
        """验证 PlanExecutionPolicy 拒绝时不调用 runner，且 emit 完整协议事件。

        核心断言：
        1. runner 从未被调用（拒绝发生在 runner 之前）
        2. 返回 ToolCallResult，is_error=True
        3. result JSON 包含 plan_mode_denied=True
        4. ToolStart + ToolComplete 事件正常 emit（UI 面板能看到被拒绝状态）
        """
        calls = []

        def runner(name, args):
            calls.append((name, args))
            raise AssertionError("denied plan-mode tool should not execute")

        ex = ToolExecutor(runner, self.bus)
        results = ex.execute(
            [_tc("bash", '{"command":"rm file"}', call_id="call_plan_deny")],
            round_idx=3,
            execution_policy=PlanExecutionPolicy(),
        )

        # 断言 1: runner 从未被调用
        self.assertEqual(calls, [])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_error)
        # 断言 3: 拒绝 payload 完整
        payload = json.loads(results[0].result)
        self.assertTrue(payload["plan_mode_denied"])
        self.assertEqual(payload["tool"], "bash")

        # 断言 4: 协议事件正常 emit
        starts = [e for e in self.events if isinstance(e, ToolStart)]
        completes = [e for e in self.events if isinstance(e, ToolComplete)]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(completes), 1)
        self.assertEqual(starts[0].call_id, "call_plan_deny")
        self.assertEqual(completes[0].call_id, "call_plan_deny")
        self.assertTrue(completes[0].is_error)


# ========== ToolExecutor 并行 ==========


class TestExecutorParallel(unittest.TestCase):
    def test_parallel_actually_concurrent(self):
        """3 个 0.1s 工具，并发应在 ~0.1s 内完成而非 0.3s。"""

        def slow_runner(name, args):
            time.sleep(0.1)
            return "{}"

        ex = ToolExecutor(slow_runner, max_workers=4)
        start = time.perf_counter()
        results = ex.execute([
            _tc("file_read", call_id="a"),
            _tc("search", call_id="b"),
            _tc("memory_search", call_id="c"),
        ])
        elapsed = time.perf_counter() - start

        # 给 50% buffer
        self.assertLess(elapsed, 0.18, f"并发 elapsed={elapsed:.3f}s 不像并发跑")
        self.assertEqual([r.call_id for r in results], ["a", "b", "c"])

    def test_parallel_keeps_input_order(self):
        """worker 完成顺序乱序，结果列表仍按输入顺序。"""

        def vary_runner(name, args):
            # 第一个工具睡得最久，故意打乱完成顺序
            sleep_for = {"file_read": 0.08, "search": 0.02, "memory_search": 0.04}
            time.sleep(sleep_for.get(name, 0))
            return "{}"

        ex = ToolExecutor(vary_runner, max_workers=4)
        results = ex.execute([
            _tc("file_read", call_id="1"),
            _tc("search", call_id="2"),
            _tc("memory_search", call_id="3"),
        ])
        self.assertEqual([r.call_id for r in results], ["1", "2", "3"])

    def test_serial_when_mixed(self):
        """含 bash 时退回串行，证据：runner 没有并发执行。"""

        running = [0]
        max_concurrent = [0]
        lock = threading.Lock()

        def gated_runner(name, args):
            with lock:
                running[0] += 1
                max_concurrent[0] = max(max_concurrent[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return "{}"

        ex = ToolExecutor(gated_runner)
        ex.execute([
            _tc("file_read"), _tc("bash"), _tc("search"),
        ])
        self.assertEqual(max_concurrent[0], 1, "含 bash 时不应并发")


# ========== CancelToken + ContextVars ==========


class TestCancelTokenContextVars(unittest.TestCase):
    def test_basic_cancel(self):
        token = CancelToken()
        self.assertFalse(token.is_cancelled())
        token.cancel()
        self.assertTrue(token.is_cancelled())

    def test_event_property(self):
        token = CancelToken()
        ev = token.event
        self.assertFalse(ev.is_set())
        token.cancel()
        self.assertTrue(ev.is_set())

    def test_context_var_default_none(self):
        # 干净 context 下 get 返回 None
        self.assertIsNone(get_current_cancel_token())

    def test_set_and_get_in_same_context(self):
        token = CancelToken()
        ctx_token = set_current_cancel_token(token)
        try:
            self.assertIs(get_current_cancel_token(), token)
        finally:
            reset_current_cancel_token(ctx_token)

    def test_token_visible_in_worker_thread_via_executor(self):
        """ToolExecutor 用 contextvars.copy_context()，worker thread 内
        get_current_cancel_token 应拿到主线程绑的 token。"""
        token = CancelToken()
        seen = []

        def runner(name, args):
            seen.append(get_current_cancel_token())
            return "{}"

        ex = ToolExecutor(runner)
        ctx_token = set_current_cancel_token(token)
        try:
            ex.execute([
                _tc("file_read"), _tc("search"), _tc("memory_search"),
            ])
        finally:
            reset_current_cancel_token(ctx_token)

        self.assertEqual(len(seen), 3)
        for t in seen:
            self.assertIs(t, token)


class TestPlatformToolPermission(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = collect_all(self.bus)
        self.conversation = ConversationKey("qq", "group", "10001")

    def _run_as_sender(self, sender_id: str, tool_name: str, args_json: str, *, env=None):
        calls = []

        def runner(name, args):
            calls.append((name, args))
            return json.dumps({"ok": True}, ensure_ascii=False)

        ex = ToolExecutor(runner, self.bus)
        conv_token = set_current_platform_conversation(self.conversation)
        sender_token = set_current_platform_sender(sender_id)
        try:
            guard_env = {
                "QQ_ROOT_USERS": "",
                "IM_ROOT_USERS": "",
                "CBAGENT_MCP_PUBLIC_PREFIXES": "",
                "CBAGENT_MCP_SENSITIVE_PREFIXES": "",
            }
            guard_env.update(env or {})
            with patch.dict("os.environ", guard_env, clear=False):
                results = ex.execute([_tc(tool_name, args_json, call_id="call_guard")], round_idx=7)
        finally:
            reset_current_platform_sender(sender_token)
            reset_current_platform_conversation(conv_token)
        return calls, results

    def test_local_cli_context_is_not_restricted(self):
        calls = []

        def runner(name, args):
            calls.append(name)
            return "{}"

        ex = ToolExecutor(runner)
        result = ex.execute([_tc("file_write", '{"path":"a.txt","content":"x"}')])
        self.assertEqual(calls, ["file_write"])
        self.assertFalse(result[0].is_error)

    def test_non_root_qq_user_cannot_write_files(self):
        calls, results = self._run_as_sender(
            "200",
            "file_write",
            '{"path":"a.txt","content":"x"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        self.assertTrue(results[0].is_error)
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("file_write", payload["error"])

        starts = [e for e in self.events if isinstance(e, ToolStart)]
        completes = [e for e in self.events if isinstance(e, ToolComplete)]
        self.assertEqual(starts, [])
        self.assertEqual(len(completes), 1)
        self.assertTrue(completes[0].is_error)

    def test_sensitive_tools_default_deny_when_root_users_not_configured(self):
        calls, results = self._run_as_sender(
            "200",
            "file_edit",
            '{"path":"a.txt","old_string":"a","new_string":"b"}',
            env={"QQ_ROOT_USERS": "", "IM_ROOT_USERS": ""},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("不是 root 用户", payload["error"])

    def test_root_qq_user_can_run_sensitive_tool(self):
        calls, results = self._run_as_sender(
            "100",
            "file_write",
            '{"path":"a.txt","content":"x"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["file_write"])
        self.assertFalse(results[0].is_error)

    def test_im_root_users_also_grants_root_permission(self):
        calls, results = self._run_as_sender(
            "300",
            "file_write",
            '{"path":"a.txt","content":"x"}',
            env={"QQ_ROOT_USERS": "", "IM_ROOT_USERS": "300"},
        )
        self.assertEqual([name for name, _ in calls], ["file_write"])
        self.assertFalse(results[0].is_error)

    def test_file_read_and_content_grep_are_denied_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "file_read",
            '{"path":"run_agent.py"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("本地文件内容", payload["error"])

        calls, results = self._run_as_sender(
            "200",
            "grep",
            '{"pattern":"class AgentRunner","output_mode":"content"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("匹配内容", payload["error"])

    def test_grep_file_list_mode_is_allowed_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "grep",
            '{"pattern":"class AgentRunner","output_mode":"files_with_matches"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["grep"])
        self.assertFalse(results[0].is_error)

    def test_bash_task_list_allowed_but_output_denied_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "bash_task",
            '{"action":"list"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["bash_task"])
        self.assertFalse(results[0].is_error)

        calls, results = self._run_as_sender(
            "200",
            "bash_task",
            '{"action":"output","task_id":"t1"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("后台任务输出", payload["error"])

    def test_readonly_bash_is_allowed_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "bash",
            '{"command":"git status --short"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["bash"])
        self.assertFalse(results[0].is_error)

    def test_write_bash_is_denied_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "bash",
            '{"command":"git reset --hard HEAD"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("不是只读白名单", payload["error"])

    def test_bash_file_content_commands_are_denied_for_non_root(self):
        for command in ("cat run_agent.py", "Get-Content run_agent.py", "git diff"):
            with self.subTest(command=command):
                calls, results = self._run_as_sender(
                    "200",
                    "bash",
                    json.dumps({"command": command}, ensure_ascii=False),
                    env={"QQ_ROOT_USERS": "100"},
                )
                self.assertEqual(calls, [])
                payload = json.loads(results[0].result)
                self.assertTrue(payload["permission_denied"])
                self.assertIn("本地文件内容", payload["error"])

    def test_sticker_name_is_allowed_but_path_asset_is_denied(self):
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"upload_group_file","args":{"group_id":"10001","file":"https://example.com/happy.png","name":"happy.png"}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"upload_group_file","args":{"group_id":"10001","file":"/root/CBAGENT/cb-agent/run_agent.py"}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("外发任意本地文件", payload["error"])

        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"upload_group_file","args":{"group_id":"10001","file":"file:///root/CBAGENT/cb-agent/run_agent.py"}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("外发任意本地文件", payload["error"])

    def test_non_root_can_create_and_send_temp_artifact(self):
        temp_output = Path(tempfile.gettempdir()) / "cb-agent-outputs" / "report.txt"

        calls, results = self._run_as_sender(
            "200",
            "file_write",
            json.dumps({"path": str(temp_output), "content": "hello"}, ensure_ascii=False),
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["file_write"])
        self.assertFalse(results[0].is_error)

        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {"funname": "upload_group_file", "args": {"group_id": "10001", "file": str(temp_output)}},
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

    def test_qqtool_json_string_args_are_checked_like_object_args(self):
        nested_args = json.dumps({"group_id": "10001", "message": "hi"}, ensure_ascii=False)
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {"funname": "send_group_msg", "args": nested_args},
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )

        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

    def test_qqtool_current_group_id_can_be_omitted_for_current_conversation(self):
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {"funname": "send_group_msg", "args": {"message": "hi"}},
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )

        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

    def test_qqtool_message_segment_blocks_arbitrary_local_file_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {
                    "funname": "send_group_msg",
                    "args": {
                        "group_id": "10001",
                        "message": [{"type": "image", "data": {"file": "run_agent.py"}}],
                    },
                },
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )

        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("消息段可能外发任意本地文件", payload["error"])

    def test_qqtool_message_segment_allows_temp_artifact_for_non_root(self):
        temp_output = Path(tempfile.gettempdir()) / "cb-agent-outputs" / "image.png"
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {
                    "funname": "send_group_msg",
                    "args": {
                        "group_id": "10001",
                        "message": [{"type": "image", "data": {"file": str(temp_output)}}],
                    },
                },
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )

        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

    def test_qqtool_cq_message_string_resource_policy_matches_segments(self):
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {
                    "funname": "send_group_msg",
                    "args": {
                        "group_id": "10001",
                        "message": "[CQ:image,file=run_agent.py]",
                    },
                },
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )

        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("消息段可能外发任意本地文件", payload["error"])

        temp_output = Path(tempfile.gettempdir()) / "cb-agent-outputs" / "image.png"
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            json.dumps(
                {
                    "funname": "send_group_msg",
                    "args": {
                        "group_id": "10001",
                        "message": f"[CQ:image,file={temp_output}]",
                    },
                },
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )

        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

    def test_temp_symlink_to_project_is_not_treated_as_safe_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            link = Path(td) / "project-link"
            try:
                os.symlink(Path.cwd(), link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"当前平台不能创建目录软链接: {exc}")

            calls, results = self._run_as_sender(
                "200",
                "qqtool",
                json.dumps(
                    {"funname": "upload_group_file", "args": {"group_id": "10001", "file": str(link / "run_agent.py")}},
                    ensure_ascii=False,
                ),
                env={"QQ_ROOT_USERS": "100"},
            )
            self.assertEqual(calls, [])
            payload = json.loads(results[0].result)
            self.assertTrue(payload["permission_denied"])
            self.assertIn("外发任意本地文件", payload["error"])

    def test_non_root_can_download_public_url_to_temp_but_not_localhost(self):
        temp_output = Path(tempfile.gettempdir()) / "cb-agent-outputs" / "image.png"
        shell_output = temp_output.as_posix()

        calls, results = self._run_as_sender(
            "200",
            "bash",
            json.dumps(
                {"command": f"curl -o {shell_output} https://example.com/image.png"},
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["bash"])
        self.assertFalse(results[0].is_error)

        calls, results = self._run_as_sender(
            "200",
            "bash",
            json.dumps(
                {"command": f"curl -o {shell_output} http://127.0.0.1:8000/secret"},
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("不是只读白名单", payload["error"])

        calls, results = self._run_as_sender(
            "200",
            "bash",
            json.dumps(
                {"command": f"curl -L -o {shell_output} https://example.com/image.png"},
                ensure_ascii=False,
            ),
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])

    def test_non_root_cannot_copy_local_file_to_temp_with_bash(self):
        temp_output = Path(tempfile.gettempdir()) / "cb-agent-outputs" / "run_agent.py"
        calls, results = self._run_as_sender(
            "200",
            "bash",
            json.dumps({"command": f"cp run_agent.py {temp_output}"}, ensure_ascii=False),
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("不是只读白名单", payload["error"])

    def test_run_skill_script_is_denied_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "run_skill_script",
            '{"skill":"demo","script":"run.py"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("skill 脚本", payload["error"])

    def test_public_mcp_allowed_sensitive_mcp_denied(self):
        calls, results = self._run_as_sender(
            "200",
            "fetch_fetch",
            '{"url":"https://example.com"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["fetch_fetch"])
        self.assertFalse(results[0].is_error)

        calls, results = self._run_as_sender(
            "200",
            "github_create_issue",
            '{"title":"x"}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("MCP", payload["error"])

    def test_qqtool_root_only_and_cross_conversation_are_denied_for_non_root(self):
        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"get_friend_list","args":{}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("root-only", payload["error"])

        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"send_group_msg","args":{"group_id":"999","message":"hi"}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual(calls, [])
        payload = json.loads(results[0].result)
        self.assertTrue(payload["permission_denied"])
        self.assertIn("非当前群聊", payload["error"])

        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"send_poke","args":{"user_id":"200"}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

        calls, results = self._run_as_sender(
            "200",
            "qqtool",
            '{"funname":"send_poke","args":{"group_id":"10001","user_id":"200"}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)

    def test_qqtool_root_user_can_run_sensitive_funname(self):
        calls, results = self._run_as_sender(
            "100",
            "qqtool",
            '{"funname":"get_friend_list","args":{}}',
            env={"QQ_ROOT_USERS": "100"},
        )
        self.assertEqual([name for name, _ in calls], ["qqtool"])
        self.assertFalse(results[0].is_error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
