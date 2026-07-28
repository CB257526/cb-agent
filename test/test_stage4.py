"""Stage 4 单测：CancelToken 集成与 chat_async。

CancelToken 路径：
- AgentSession 在 think 之前看到 token.cancelled → 直接收尾，不再 think
- 流式中途被 cancel（FakeLLM 返回带 cancelled 的 result）→ 不再发新轮
- ToolExecutor 串行 / 并行两条路径下，cancel 时未跑工具回占位

chat_async：
- 跑通基本 chat
- 外部 await 期间，cancel_token.cancel() 能让 chat 返回

"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent.cancel import CancelToken
from agent.event_bus import EventBus
from agent.events import Cancelled, Done, RoundEnd, RoundStart, ToolComplete
from agent.executor import ToolExecutor, should_parallelize
from agent.session import AgentSession


# ========== Fakes ==========


class FakeLLM:
    """可控 think 返回的假 LLM。每次 think 取 outputs.pop(0)。"""

    is_Function_Calling = True
    model = "fake"

    def __init__(self, outputs: List[Any], on_think=None):
        self.outputs = list(outputs)
        self.calls: List[Dict[str, Any]] = []
        self.on_think = on_think  # 给测试一个 hook 在 think 内做事（如 cancel）

    def think(self, messages, tools=None, event_bus=None,
              cancel_event: Optional[threading.Event] = None, round_idx: int = 0):
        self.calls.append({
            "round": round_idx,
            "cancel_event_set": cancel_event.is_set() if cancel_event else None,
        })
        if self.on_think is not None:
            self.on_think(round_idx, cancel_event)
        if not self.outputs:
            return {"answer": "", "tool_calls": []}
        return self.outputs.pop(0)


def _make_session(llm: FakeLLM, runner=None) -> tuple[AgentSession, EventBus, List[Any]]:
    bus = EventBus()
    events: List[Any] = []
    bus.subscribe(events.append)

    reg = MagicMock()
    reg.execute_tool = MagicMock(side_effect=runner if runner else lambda n, a: "{}")
    reg.get_tools_description_openai_schema = MagicMock(return_value=[])
    reg.get_tools_description = MagicMock(return_value="")

    ex = ToolExecutor(reg.execute_tool, bus)
    s = AgentSession(
        llm=llm, registry=reg, executor=ex, event_bus=bus,
        memory_loader=None, skill_manager=None, ctx_enabled=False,
    )
    return s, bus, events


# ========== AgentSession + CancelToken ==========


class TestSessionCancel(unittest.TestCase):

    def test_cancel_before_chat_returns_immediately(self):
        """token 进 chat 前已被 cancel → 不调 think，直接收尾，Done.cancelled=True。"""
        llm = FakeLLM([{"answer": "x", "tool_calls": []}])
        s, _, events = _make_session(llm)

        token = CancelToken()
        token.cancel()
        ans = s.chat("hi", cancel_token=token)

        self.assertEqual(ans, "")
        self.assertEqual(len(llm.calls), 0)  # think 一次没调
        cancelled = [e for e in events if isinstance(e, Cancelled)]
        self.assertGreaterEqual(len(cancelled), 1)
        done = [e for e in events if isinstance(e, Done)]
        self.assertEqual(len(done), 1)
        self.assertTrue(done[0].cancelled)

    def test_cancel_mid_stream_no_more_rounds(self):
        """think 第 1 轮内调 cancel；本轮已经返回了 tool_calls 也不再跑下一轮。"""
        def cancel_during_think(round_idx, cancel_event):
            if cancel_event is not None:
                cancel_event.set()

        llm = FakeLLM(
            [
                {"answer": "中途", "tool_calls": [
                    {"id": "c1", "type": "function",
                     "function": {"name": "search", "arguments": "{}"}},
                ]},
                {"answer": "应该不会到这", "tool_calls": []},
            ],
            on_think=cancel_during_think,
        )
        s, _, events = _make_session(llm)
        token = CancelToken()
        ans = s.chat("hi", cancel_token=token)

        # think 只调了 1 次（第 2 轮被 cancel 阻断）
        self.assertEqual(len(llm.calls), 1)
        # answer 是流式中已经收到的部分
        self.assertEqual(ans, "中途")
        done = [e for e in events if isinstance(e, Done)]
        self.assertTrue(done[0].cancelled)

    def test_cancel_during_chat_via_current_token(self):
        """另一个线程通过 session.current_cancel_token 拿到 token 后 cancel。"""
        cancel_done = threading.Event()

        def chat_running_think(round_idx, cancel_event):
            # 模拟一个慢 think：等外部触发 cancel 再返回
            cancel_done.wait(timeout=2.0)

        llm = FakeLLM(
            [{"answer": "", "tool_calls": []}],
            on_think=chat_running_think,
        )
        s, _, events = _make_session(llm)

        canceller_started = threading.Event()
        def canceller():
            # 等 session 进入 chat 后再 cancel
            for _ in range(50):
                if s.current_cancel_token is not None:
                    break
                threading.Event().wait(0.02)
            canceller_started.set()
            s.current_cancel_token.cancel()
            cancel_done.set()

        t = threading.Thread(target=canceller)
        t.start()
        ans = s.chat("hi")
        t.join(timeout=3.0)

        self.assertTrue(canceller_started.is_set())
        # current_cancel_token 在 chat 返回后必须清空
        self.assertIsNone(s.current_cancel_token)


# ========== ToolExecutor + CancelToken ==========


class TestExecutorCancel(unittest.TestCase):

    def _tc(self, tid: str, name: str, args: str = "{}"):
        return {"id": tid, "type": "function",
                "function": {"name": name, "arguments": args}}

    def test_serial_cancel_skips_remaining(self):
        """串行：第一个工具跑完后 cancel，第二个变占位。"""
        bus = EventBus()
        events: List[Any] = []
        bus.subscribe(events.append)
        token = CancelToken()
        call_count = {"n": 0}

        def runner(name, args):
            call_count["n"] += 1
            if call_count["n"] == 1:
                token.cancel()  # 第一个工具完后 cancel
            return "{}"

        # 两个工具其中一个不是只读 → 串行
        ex = ToolExecutor(runner, bus)
        results = ex.execute(
            [self._tc("c1", "bash"), self._tc("c2", "bash")],
            round_idx=1, cancel_token=token,
        )
        self.assertEqual(call_count["n"], 1)
        self.assertEqual(len(results), 2)
        # 第二条未开始，必须得到成对的取消终态。
        self.assertTrue(results[1].is_error)
        self.assertIn("cancelled_before_start", results[1].result)
        # 执行器只发工具终态；整个回合的 Cancelled 由 Session 落盘后统一发送。
        terminal = [
            event for event in events
            if isinstance(event, ToolComplete) and event.call_id == "c2"
        ]
        self.assertEqual(len(terminal), 1)

    def test_pre_submit_cancel_all_placeholders(self):
        """execute 入口已 cancel → 全部占位，runner 一次都不调。"""
        bus = EventBus()
        token = CancelToken()
        token.cancel()

        called = {"n": 0}
        def runner(name, args):
            called["n"] += 1
            return "{}"

        ex = ToolExecutor(runner, bus)
        results = ex.execute(
            [self._tc("c1", "search"), self._tc("c2", "search")],
            round_idx=1, cancel_token=token,
        )
        self.assertEqual(called["n"], 0)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(r.is_error and "cancelled" in r.result for r in results))

    def test_parallel_completes_normally_when_not_cancelled(self):
        """并发模式无 cancel，全部正常跑（保留旧行为）。"""
        bus = EventBus()
        token = CancelToken()
        ex = ToolExecutor(lambda n, a: '{"ok":1}', bus)

        # 两个只读 → 并发
        calls = [self._tc("c1", "search"), self._tc("c2", "file_read")]
        self.assertTrue(should_parallelize(calls))
        results = ex.execute(calls, round_idx=1, cancel_token=token)
        self.assertEqual(len(results), 2)
        self.assertFalse(any(r.is_error for r in results))


# ========== chat_async ==========


class TestChatAsync(unittest.TestCase):

    def test_chat_async_basic(self):
        llm = FakeLLM([{"answer": "你好", "tool_calls": []}])
        s, _, _ = _make_session(llm)

        ans = asyncio.run(s.chat_async("hi"))
        self.assertEqual(ans, "你好")

    def test_chat_async_cancel_via_token(self):
        """正在 await chat_async 时主协程 cancel token，chat 收尾正常返回。"""
        block = threading.Event()

        def slow_think(round_idx, cancel_event):
            # 等到 cancel 被外部触发再返回
            block.wait(timeout=2.0)

        llm = FakeLLM([{"answer": "", "tool_calls": []}], on_think=slow_think)
        s, _, events = _make_session(llm)

        async def main():
            token = CancelToken()
            task = asyncio.create_task(s.chat_async("hi", cancel_token=token))
            # 等 chat 进入 think
            for _ in range(50):
                await asyncio.sleep(0.01)
                if s.current_cancel_token is not None:
                    break
            token.cancel()
            block.set()  # 让 think 返回
            return await asyncio.wait_for(task, timeout=2.0)

        ans = asyncio.run(main())
        # ans 可能是空串；关键是不卡住、不抛
        self.assertIsInstance(ans, str)

if __name__ == "__main__":
    unittest.main(verbosity=2)
