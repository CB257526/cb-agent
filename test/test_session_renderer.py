"""AgentSession + CLIRenderer 单测。

不依赖真实 OpenAI API；用 fake LLM / fake registry 验流程和事件。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import threading
import unittest
import json
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

# 让单测从任意 cwd 都能 import（test/ 下直接跑、cb-agent/ 下跑都行）
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

from agent.event_bus import EventBus, collect_all
from agent.events import (
    BackgroundNotification, Cancelled, Done, Error, ReasoningDelta,
    RoundEnd, RoundStart, TextDelta, TokenUsage, ToolComplete, ToolStart,
)
from agent.executor import ToolExecutor
from agent.renderers.cli import (
    CLIRenderer, _render_thought, _render_todo_panel, _render_bash_output,
    _short_args,
)
from agent.session import AgentSession
from agent.work_context import LocalSessionStore


# ========== fakes ==========


class FakeLLM:
    """模拟 CbAgentsLLM.think 行为。

    用法：构造时给一组 results；每次 think 按顺序返回。
    每次 think 时会按需 emit 流式事件。
    """

    def __init__(self, results: List[Any], emit_text: bool = False, emit_reasoning: bool = False) -> None:
        self.results = list(results)
        self.is_Function_Calling = True
        self.model = "fake"
        self.emit_text = emit_text
        self.emit_reasoning = emit_reasoning
        self.calls: List[Dict[str, Any]] = []

    def think(self, messages, tools=None, event_bus=None, round_idx=0, cancel_event=None):
        self.calls.append({
            "messages": list(messages),
            "tools": tools,
            "round_idx": round_idx,
            "had_bus": event_bus is not None,
        })
        if not self.results:
            raise AssertionError("FakeLLM.think 调用次数超出 results")
        result = self.results.pop(0)
        # 模拟流式 emit：reasoning 先于 text
        if event_bus is not None:
            if self.emit_reasoning:
                event_bus.emit(ReasoningDelta(delta="思考中…", accumulated="思考中…", round_idx=round_idx))
            answer = result.get("answer", "") if isinstance(result, dict) else ""
            if self.emit_text and answer:
                event_bus.emit(TextDelta(delta=answer, accumulated=answer, round_idx=round_idx))
        return result


def _tc(name: str, args: str = "{}", call_id: str = "") -> dict:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


# ========== AgentSession ==========


class TestAgentSessionBasic(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()
        self.events = collect_all(self.bus)
        self.registry = MagicMock()
        self.registry.execute_tool = MagicMock(return_value="{}")
        self.registry.get_tools_description_openai_schema = MagicMock(return_value=[])
        self.registry.get_tools_description = MagicMock(return_value="")
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)

    def _make_session(self, llm: FakeLLM, **kwargs) -> AgentSession:
        return AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, builder=None, skill_manager=None,
            ctx_enabled=False, **kwargs,
        )

    def test_chat_no_tool_calls_returns_answer(self):
        llm = FakeLLM([{"answer": "你好", "tool_calls": []}])
        s = self._make_session(llm)
        ans = s.chat("hi")
        self.assertEqual(ans, "你好")
        self.assertEqual(len(llm.calls), 1)

    def test_chat_emits_round_start_end_and_done(self):
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
        s = self._make_session(llm)
        s.chat("q")

        starts = [e for e in self.events if isinstance(e, RoundStart)]
        ends = [e for e in self.events if isinstance(e, RoundEnd)]
        dones = [e for e in self.events if isinstance(e, Done)]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0].final)
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0].final_answer, "ok")
        self.assertEqual(dones[0].rounds_used, 1)

    def test_chat_with_tool_call_runs_two_rounds(self):
        # 第 1 轮：模型让调 file_read；第 2 轮：模型给最终答案
        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("file_read", '{"path":"a.txt"}')]},
            {"answer": "看到了 abc", "tool_calls": []},
        ])
        self.registry.execute_tool = MagicMock(return_value='{"content":"abc"}')
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
        )
        ans = s.chat("读 a.txt")
        self.assertEqual(ans, "看到了 abc")
        self.assertEqual(len(llm.calls), 2)
        # 第 2 次 think 的 messages 里应有 tool 消息
        round2_msgs = llm.calls[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" for m in round2_msgs))

    def test_tool_trace_appended_as_work_record_and_seen_next_turn(self):
        long_content = "abcdef" * 40
        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("file_read", '{"path":"a.txt"}')]},
            {"answer": "看到了文件", "tool_calls": []},
            {"answer": "继续", "tool_calls": []},
        ])
        self.registry.execute_tool = MagicMock(return_value=json.dumps({
            "path": "a.txt",
            "mode": "head-100",
            "total_lines": 12,
            "returned_lines": 12,
            "truncated": False,
            "content": long_content,
        }, ensure_ascii=False))
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)

        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("读 a.txt")

            self.assertEqual(len(s.history), 3)
            work_content = s.history[-1].content
            self.assertIsInstance(work_content, str)
            self.assertIn("【工作记录】", work_content)
            self.assertIn("a.txt", work_content)
            self.assertNotIn(long_content, work_content)

            round2_tool_msgs = llm.calls[1]["messages"]
            self.assertTrue(any(
                m.get("role") == "tool" and long_content in m.get("content", "")
                for m in round2_tool_msgs
            ))

            transcript = store.active_dir / "transcript.jsonl"
            raw_transcript = transcript.read_text(encoding="utf-8")
            self.assertIn("【工作记录】", raw_transcript)
            self.assertNotIn(long_content, raw_transcript)

            s.chat("继续分析")
            next_turn_messages = llm.calls[2]["messages"]
            self.assertTrue(any(
                "【工作记录】" in str(m.get("content", ""))
                for m in next_turn_messages
            ))

    def test_session_store_restores_history_and_clear_deletes_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            llm = FakeLLM([
                {"answer": "", "tool_calls": [_tc("file_read", '{"path":"b.txt"}')]},
                {"answer": "done", "tool_calls": []},
            ])
            self.registry.execute_tool = MagicMock(return_value=json.dumps({
                "path": "b.txt",
                "mode": "head-100",
                "total_lines": 1,
                "returned_lines": 1,
                "content": "hello",
            }, ensure_ascii=False))
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("读 b.txt")
            active_dir = store.active_dir

            restored = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            self.assertEqual(len(restored.history), 3)
            self.assertIn("【工作记录】", str(restored.history[-1].content))

            restored.clear_history()
            self.assertEqual(restored.history, [])
            self.assertFalse(active_dir.exists())
            self.assertFalse((root / "index.json").exists())

    def test_chat_history_appended_correctly(self):
        llm = FakeLLM([{"answer": "好的", "tool_calls": []}])
        s = self._make_session(llm)
        s.chat("hello")
        self.assertEqual(len(s.history), 2)
        # user + assistant
        roles = [m.role.value if hasattr(m.role, "value") else str(m.role) for m in s.history]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_chat_history_empty_when_no_answer(self):
        # 模型没给 answer 也没给 tool_calls 但走到了非预期分支
        llm = FakeLLM([{"answer": "", "tool_calls": []}])
        s = self._make_session(llm)
        ans = s.chat("test")
        self.assertEqual(ans, "")
        # user 入了，assistant 因为空字符串没入
        self.assertEqual(len(s.history), 1)

    def test_clear_history(self):
        llm = FakeLLM([{"answer": "a", "tool_calls": []}])
        s = self._make_session(llm)
        s.chat("q")
        self.assertEqual(len(s.history), 2)
        s.clear_history()
        self.assertEqual(len(s.history), 0)

    def test_messages_snapshot_hook_called_each_round(self):
        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("file_read")]},
            {"answer": "done", "tool_calls": []},
        ])
        self.registry.execute_tool = MagicMock(return_value="{}")
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        snapshots: List[tuple[int, int]] = []

        def hook(msgs, round_idx):
            snapshots.append((round_idx, len(msgs)))

        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
            messages_snapshot_hook=hook,
        )
        s.chat("q")
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0][0], 1)
        self.assertEqual(snapshots[1][0], 2)
        # 第 2 轮 messages 比第 1 轮多（多了 assistant + tool）
        self.assertGreater(snapshots[1][1], snapshots[0][1])

    def test_messages_snapshot_hook_exception_swallowed(self):
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])

        def bad_hook(msgs, round_idx):
            raise RuntimeError("hook boom")

        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
            messages_snapshot_hook=bad_hook,
        )
        ans = s.chat("q")
        self.assertEqual(ans, "ok")  # 仍然正常返回

    def test_max_rounds_emits_error(self):
        # 永远让模型继续要工具
        infinite = [
            {"answer": "", "tool_calls": [_tc("file_read")]}
            for _ in range(AgentSession.MAX_TOOL_ROUNDS + 1)
        ]
        llm = FakeLLM(infinite)
        self.registry.execute_tool = MagicMock(return_value="{}")
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
        )
        ans = s.chat("q")
        self.assertIn("终止", ans)
        errors = [e for e in self.events if isinstance(e, Error)]
        self.assertTrue(any(err.where == "session" for err in errors))

    def test_unsupported_fc_returns_text(self):
        # think 在不支持 FC 时返回 [text, None]
        llm = FakeLLM([["纯文本回答", None]])
        s = self._make_session(llm)
        ans = s.chat("q")
        self.assertEqual(ans, "纯文本回答")

    def test_unexpected_result_emits_error(self):
        llm = FakeLLM(["bad string result"])
        s = self._make_session(llm)
        ans = s.chat("q")
        self.assertEqual(ans, "")
        errors = [e for e in self.events if isinstance(e, Error)]
        self.assertTrue(any(err.where == "llm" for err in errors))


# ========== CLIRenderer ==========


class TestRenderHelpers(unittest.TestCase):
    def test_short_args_short(self):
        s = _short_args({"a": 1})
        self.assertIn("a", s)
        self.assertLess(len(s), 80)

    def test_short_args_truncated(self):
        s = _short_args({"x": "a" * 200})
        self.assertTrue(s.endswith("..."))
        self.assertLessEqual(len(s), 80)

    def test_render_thought_with_elapsed(self):
        out = _render_thought("分析下", elapsed_seconds=2.5)
        self.assertIn("Thought", out)
        self.assertIn("2.5s", out)
        self.assertIn("分析下", out)

    def test_render_todo_panel_basic(self):
        out = _render_todo_panel('{"todos": [{"id": "1", "content": "task A", "status": "pending"}]}')
        self.assertIsNotNone(out)
        self.assertIn("Update Todos", out)
        self.assertIn("task A", out)

    def test_render_todo_panel_invalid_json(self):
        self.assertIsNone(_render_todo_panel("not json"))

    def test_render_todo_panel_wrong_shape(self):
        self.assertIsNone(_render_todo_panel('{"foo":"bar"}'))

    def test_render_bash_silent_done(self):
        out = _render_bash_output(
            '{"stdout":"","stderr":"","exit_code":0,"is_error":false,'
            '"classification":{"kind":"silent"}}'
        )
        self.assertIsNotNone(out)
        self.assertIn("Done", out)

    def test_render_bash_invalid_json(self):
        self.assertIsNone(_render_bash_output("not json"))


class TestCLIRenderer(unittest.TestCase):
    def _capture_chat(self, llm: FakeLLM) -> str:
        bus = EventBus()
        registry = MagicMock()
        registry.execute_tool = MagicMock(return_value="{}")
        registry.get_tools_description_openai_schema = MagicMock(return_value=[])
        registry.get_tools_description = MagicMock(return_value="")
        executor = ToolExecutor(registry.execute_tool, bus)
        renderer = CLIRenderer(bus)
        renderer.attach()
        session = AgentSession(
            llm=llm, registry=registry, executor=executor,
            event_bus=bus, ctx_enabled=False,
        )
        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            session.chat("q")
        renderer.detach()
        return buf_out.getvalue() + buf_err.getvalue()

    def test_renders_round_start(self):
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
        out = self._capture_chat(llm)
        self.assertIn("[round 1]", out)

    def test_renders_streaming_text(self):
        llm = FakeLLM(
            [{"answer": "你好世界", "tool_calls": []}],
            emit_text=True,
        )
        out = self._capture_chat(llm)
        self.assertIn("assistant >", out)
        self.assertIn("你好世界", out)

    def test_renders_thought_after_round_end(self):
        llm = FakeLLM(
            [{"answer": "答", "tool_calls": []}],
            emit_text=True,
            emit_reasoning=True,
        )
        out = self._capture_chat(llm)
        self.assertIn("Thought", out)
        self.assertIn("思考中", out)

    def test_renders_tool_start_and_complete(self):
        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("file_read", '{"path":"a"}')]},
            {"answer": "done", "tool_calls": []},
        ])
        out = self._capture_chat(llm)
        self.assertIn("调用工具", out)
        self.assertIn("file_read", out)

    def test_renders_error(self):
        bus = EventBus()
        renderer = CLIRenderer(bus)
        renderer.attach()
        buf = io.StringIO()
        with redirect_stderr(buf):
            bus.emit(Error(where="test", message="炸了", round_idx=1))
        renderer.detach()
        self.assertIn("炸了", buf.getvalue())

    def test_renders_cancelled(self):
        bus = EventBus()
        renderer = CLIRenderer(bus)
        renderer.attach()
        buf = io.StringIO()
        with redirect_stdout(buf):
            bus.emit(Cancelled(where="user", round_idx=1))
        renderer.detach()
        self.assertIn("已取消", buf.getvalue())

    def test_renders_background(self):
        bus = EventBus()
        renderer = CLIRenderer(bus)
        renderer.attach()
        buf = io.StringIO()
        with redirect_stdout(buf):
            bus.emit(BackgroundNotification(
                task_id="t1", status="completed", exit_code=0, output_path="out.txt",
            ))
        renderer.detach()
        self.assertIn("后台任务", buf.getvalue())
        self.assertIn("t1", buf.getvalue())

    def test_token_usage_default_hidden(self):
        bus = EventBus()
        renderer = CLIRenderer(bus, show_token_usage=False)
        renderer.attach()
        buf = io.StringIO()
        with redirect_stdout(buf):
            bus.emit(TokenUsage(
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                round_idx=1,
            ))
        renderer.detach()
        self.assertEqual(buf.getvalue(), "")

    def test_token_usage_visible_when_enabled(self):
        bus = EventBus()
        renderer = CLIRenderer(bus, show_token_usage=True)
        renderer.attach()
        buf = io.StringIO()
        with redirect_stdout(buf):
            bus.emit(TokenUsage(
                prompt_tokens=10, completion_tokens=5, total_tokens=15,
                round_idx=1,
            ))
        renderer.detach()
        self.assertIn("tokens", buf.getvalue())
        self.assertIn("15", buf.getvalue())

    def test_attach_idempotent(self):
        bus = EventBus()
        renderer = CLIRenderer(bus)
        renderer.attach()
        renderer.attach()  # 应当先 detach 再 attach，订阅数不双倍
        # 每个事件类型只订阅一次：每个类型的订阅者数应该恰是 1
        n = bus.subscriber_count
        renderer.attach()
        self.assertEqual(bus.subscriber_count, n)
        renderer.detach()

    def test_detach_clears_subs(self):
        bus = EventBus()
        renderer = CLIRenderer(bus)
        renderer.attach()
        self.assertGreater(bus.subscriber_count, 0)
        renderer.detach()
        self.assertEqual(bus.subscriber_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
