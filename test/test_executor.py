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
            _tc("file_read"), _tc("search"), _tc("memory_search"),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
