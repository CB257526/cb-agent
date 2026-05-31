"""AskUserQuestionTool 单测：三层覆盖

- QuestionRegistry：register / submit_answer / wait_for_answer / 取消 / 超时
- AskUserQuestionTool：参数校验 / 单选 / 多选 / Other / 取消
- Gateway RPC：session.answer_question 路由到 registry，端到端唤醒工具线程
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import unittest
from typing import Any, Dict, List
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

from agent.cancel import CancelToken, set_current_cancel_token, reset_current_cancel_token
from agent.event_bus import EventBus
from agent.events import AskUserQuestion, AskUserQuestionAnswered
from agent.executor import ToolExecutor
from agent.question_registry import QuestionRegistry
from agent.session import AgentSession
from agent.transport import Gateway, StdioTransport
from tools.tools.ask_user_question_tool import AskUserQuestionTool


# ========== QuestionRegistry ==========


class TestQuestionRegistry(unittest.TestCase):

    def test_register_and_submit(self):
        reg = QuestionRegistry()
        qid = reg.new_question_id()
        reg.register(qid)

        # 另一个线程模拟 UI 回灌
        def submitter():
            time.sleep(0.05)
            reg.submit_answer(qid, selected_labels=["A"], other_text=None)

        t = threading.Thread(target=submitter)
        t.start()
        slot = reg.wait_for_answer(qid)
        t.join()

        self.assertEqual(slot.selected_labels, ["A"])
        self.assertFalse(slot.cancelled)

    def test_submit_unknown_qid_returns_false(self):
        reg = QuestionRegistry()
        ok = reg.submit_answer("nope", selected_labels=["A"])
        self.assertFalse(ok)

    def test_cancel_event_unblocks_wait(self):
        reg = QuestionRegistry()
        qid = reg.new_question_id()
        reg.register(qid)

        cancel = threading.Event()
        result = {}

        def waiter():
            slot = reg.wait_for_answer(qid, cancel_event=cancel)
            result["cancelled"] = slot.cancelled

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.05)
        cancel.set()
        t.join(timeout=2.0)

        self.assertTrue(result.get("cancelled"))

    def test_timeout(self):
        reg = QuestionRegistry()
        qid = reg.new_question_id()
        reg.register(qid)

        slot = reg.wait_for_answer(qid, timeout=0.1)
        self.assertTrue(slot.cancelled)


# ========== Tool 直接调 run() ==========


class TestAskUserQuestionTool(unittest.TestCase):

    def _make_tool(self):
        bus = EventBus()
        reg = QuestionRegistry()
        events: List[Any] = []
        bus.subscribe(events.append)
        tool = AskUserQuestionTool(question_registry=reg, event_bus=bus)
        return tool, reg, bus, events

    def test_invalid_params_returns_error_json(self):
        tool, _, _, _ = self._make_tool()
        # 选项不足
        out = tool.run({"question": "x?", "options": [{"label": "a", "description": ""}]})
        self.assertIn("error", json.loads(out))

    def test_single_select_happy_path(self):
        tool, reg, _, events = self._make_tool()

        def submitter():
            # 等 emit 发生（轮询事件列表）
            for _ in range(50):
                qids = [e.question_id for e in events if isinstance(e, AskUserQuestion)]
                if qids:
                    reg.submit_answer(qids[0], selected_labels=["B"])
                    return
                time.sleep(0.02)

        t = threading.Thread(target=submitter)
        t.start()

        out = tool.run({
            "question": "Pick one?",
            "options": [
                {"label": "A", "description": "first"},
                {"label": "B", "description": "second"},
            ],
        })
        t.join()

        payload = json.loads(out)
        self.assertEqual(payload["question"], "Pick one?")
        self.assertEqual(payload["answer"], "B")

        # 应当 emit 了 AskUserQuestion + AskUserQuestionAnswered
        types = [type(e).__name__ for e in events]
        self.assertIn("AskUserQuestion", types)
        self.assertIn("AskUserQuestionAnswered", types)

    def test_multi_select(self):
        tool, reg, _, events = self._make_tool()

        def submitter():
            for _ in range(50):
                qids = [e.question_id for e in events if isinstance(e, AskUserQuestion)]
                if qids:
                    reg.submit_answer(qids[0], selected_labels=["A", "C"])
                    return
                time.sleep(0.02)

        t = threading.Thread(target=submitter)
        t.start()
        out = tool.run({
            "question": "Pick many?",
            "options": [
                {"label": "A", "description": ""},
                {"label": "B", "description": ""},
                {"label": "C", "description": ""},
            ],
            "multi_select": True,
        })
        t.join()
        payload = json.loads(out)
        self.assertEqual(payload["answers"], ["A", "C"])

    def test_other_text_passes_through(self):
        tool, reg, _, events = self._make_tool()

        def submitter():
            for _ in range(50):
                qids = [e.question_id for e in events if isinstance(e, AskUserQuestion)]
                if qids:
                    reg.submit_answer(qids[0], selected_labels=["Other"], other_text="自定义答案")
                    return
                time.sleep(0.02)

        t = threading.Thread(target=submitter)
        t.start()
        out = tool.run({
            "question": "Q?",
            "options": [
                {"label": "A", "description": ""},
                {"label": "B", "description": ""},
            ],
        })
        t.join()
        payload = json.loads(out)
        self.assertEqual(payload["answer"], "Other")
        self.assertEqual(payload["other_text"], "自定义答案")

    def test_cancel_via_token(self):
        tool, _, _, events = self._make_tool()
        token = CancelToken()
        ctx = set_current_cancel_token(token)
        try:
            def canceller():
                time.sleep(0.05)
                token.cancel()
            t = threading.Thread(target=canceller)
            t.start()
            out = tool.run({
                "question": "Q?",
                "options": [
                    {"label": "A", "description": ""},
                    {"label": "B", "description": ""},
                ],
            })
            t.join()
        finally:
            reset_current_cancel_token(ctx)

        payload = json.loads(out)
        self.assertTrue(payload.get("cancelled"))


# ========== Gateway 端到端 ==========


class _PipeStdin:
    def __init__(self):
        self._lines: List[str] = []
        self._cv = threading.Condition()
        self._closed = False

    def push(self, line: str):
        with self._cv:
            self._lines.append(line)
            self._cv.notify_all()

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def __iter__(self): return self

    def __next__(self):
        with self._cv:
            while not self._lines and not self._closed:
                self._cv.wait()
            if self._lines:
                return self._lines.pop(0)
            raise StopIteration


class FakeLLM:
    is_Function_Calling = True
    model = "fake"
    def think(self, messages, tools=None, event_bus=None, cancel_event=None, round_idx=0):
        return {"answer": "", "tool_calls": []}


class TestGatewayAnswerQuestion(unittest.TestCase):

    def test_answer_question_routes_to_registry(self):
        bus = EventBus()
        reg = MagicMock()
        reg.execute_tool = MagicMock(return_value="{}")
        reg.get_tools_description_openai_schema = MagicMock(return_value=[])
        reg.get_tools_description = MagicMock(return_value="")
        ex = ToolExecutor(reg.execute_tool, bus)
        session = AgentSession(
            llm=FakeLLM(), registry=reg, executor=ex, event_bus=bus,
            builder=None, skill_manager=None, ctx_enabled=False,
        )

        # 预先在 registry 挂一个等待槽
        qid = session.question_registry.new_question_id()
        session.question_registry.register(qid)

        stdin = _PipeStdin()
        out_lock = threading.Lock()
        out = io.StringIO()
        cv = threading.Condition()
        parsed: List[Dict[str, Any]] = []

        class Capturing:
            def write(self_, s):
                with out_lock:
                    out.write(s)
                with cv:
                    parsed.clear()
                    for line in out.getvalue().splitlines():
                        line = line.strip()
                        if line:
                            try:
                                parsed.append(json.loads(line))
                            except Exception:
                                pass
                    cv.notify_all()
                return len(s)
            def flush(self_): pass

        transport = StdioTransport(stdin=stdin, stdout=Capturing())  # type: ignore[arg-type]
        gw = Gateway(session=session, event_bus=bus, transport=transport,
                     redirect_stdout_to_stderr=False)
        t = threading.Thread(target=gw.serve_forever, daemon=True)
        t.start()

        # 等 ready
        deadline = time.time() + 2.0
        with cv:
            while time.time() < deadline:
                if any(m.get("params", {}).get("type") == "gateway_ready" for m in parsed):
                    break
                cv.wait(timeout=0.05)

        # 准备一个等待答案的线程（模拟工具线程）
        slot_holder: Dict[str, Any] = {}
        def waiter():
            slot_holder["slot"] = session.question_registry.wait_for_answer(qid)
        wt = threading.Thread(target=waiter)
        wt.start()

        # 投 RPC
        stdin.push(json.dumps({
            "jsonrpc": "2.0", "id": "ans1",
            "method": "session.answer_question",
            "params": {"question_id": qid, "selected_labels": ["X"]},
        }) + "\n")

        # 等响应
        deadline = time.time() + 2.0
        with cv:
            while time.time() < deadline:
                if any(m.get("id") == "ans1" for m in parsed):
                    break
                cv.wait(timeout=0.05)

        wt.join(timeout=2.0)
        stdin.close()
        t.join(timeout=2.0)

        replies = [m for m in parsed if m.get("id") == "ans1"]
        self.assertEqual(len(replies), 1)
        self.assertTrue(replies[0]["result"]["delivered"])
        self.assertEqual(slot_holder["slot"].selected_labels, ["X"])

    def test_answer_question_invalid_params(self):
        bus = EventBus()
        reg = MagicMock()
        reg.execute_tool = MagicMock(return_value="{}")
        reg.get_tools_description_openai_schema = MagicMock(return_value=[])
        reg.get_tools_description = MagicMock(return_value="")
        ex = ToolExecutor(reg.execute_tool, bus)
        session = AgentSession(
            llm=FakeLLM(), registry=reg, executor=ex, event_bus=bus,
            builder=None, skill_manager=None, ctx_enabled=False,
        )

        stdin = _PipeStdin()
        out = io.StringIO()
        cv = threading.Condition()
        parsed: List[Dict[str, Any]] = []
        out_lock = threading.Lock()

        class Capturing:
            def write(self_, s):
                with out_lock:
                    out.write(s)
                with cv:
                    parsed.clear()
                    for line in out.getvalue().splitlines():
                        line = line.strip()
                        if line:
                            try:
                                parsed.append(json.loads(line))
                            except Exception:
                                pass
                    cv.notify_all()
                return len(s)
            def flush(self_): pass

        transport = StdioTransport(stdin=stdin, stdout=Capturing())  # type: ignore[arg-type]
        gw = Gateway(session=session, event_bus=bus, transport=transport,
                     redirect_stdout_to_stderr=False)
        t = threading.Thread(target=gw.serve_forever, daemon=True)
        t.start()

        deadline = time.time() + 2.0
        with cv:
            while time.time() < deadline:
                if any(m.get("params", {}).get("type") == "gateway_ready" for m in parsed):
                    break
                cv.wait(timeout=0.05)

        # 缺 question_id
        stdin.push(json.dumps({
            "jsonrpc": "2.0", "id": "bad1",
            "method": "session.answer_question",
            "params": {"selected_labels": ["A"]},
        }) + "\n")

        deadline = time.time() + 2.0
        with cv:
            while time.time() < deadline:
                if any(m.get("id") == "bad1" for m in parsed):
                    break
                cv.wait(timeout=0.05)

        stdin.close()
        t.join(timeout=2.0)

        replies = [m for m in parsed if m.get("id") == "bad1"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main(verbosity=2)
