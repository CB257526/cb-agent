"""cb_agents.CbAgentsLLM 流式事件单测。

用 mock OpenAI client 替代真实 API，验：
- TextDelta / ReasoningDelta 按 chunk 顺序发出
- accumulated 字段单调递增
- TokenUsage 在 stream 末尾 emit 一次
- ToolCallPlanned 在 tool_calls 累积完成后 emit
- cancel_event 在下个 chunk 边界中止
- 默认（无 bus）保留旧 print 行为
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from types import SimpleNamespace
from typing import List
from unittest.mock import patch

# Windows 控制台默认 GBK，cb_agents 启动时会打印 emoji；单测直接跑没走 run_agent
# 的 stdout reconfigure，这里补一下避免 UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent.cb_agents import CbAgentsLLM, _usage_to_dict
from agent.event_bus import EventBus, collect_all
from agent.events import (
    Cancelled, ReasoningDelta, TextDelta, TokenUsage, ToolCallPlanned,
)


# ========== 工具函数：构造假 chunk ==========


def _delta(content: str = "", reasoning: str = "", tool_calls=None):
    """构造 chunk.choices[0].delta。"""
    return SimpleNamespace(
        content=content or None,
        reasoning_content=reasoning or None,
        tool_calls=tool_calls,
    )


def _chunk(delta=None, usage=None):
    """构造一个 stream chunk。delta=None 表示这个 chunk 只带 usage（末尾 chunk）。"""
    if delta is None:
        return SimpleNamespace(choices=[], usage=usage)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=None)],
        usage=usage,
    )


def _tool_call_chunk(idx: int, *, id: str = "", name: str = "", args: str = "", type_: str = ""):
    """构造一个 tool_calls 分片。"""
    return SimpleNamespace(
        index=idx,
        id=id,
        type=type_,
        function=SimpleNamespace(name=name, arguments=args),
    )


def _make_llm() -> CbAgentsLLM:
    """绕开 __init__ 校验，构造一个最小可用的 LLM 实例。"""
    llm = CbAgentsLLM.__new__(CbAgentsLLM)
    llm.model = "test-model"
    llm.is_Function_Calling = True  # 子类用例可覆盖
    llm.client = SimpleNamespace()  # 不会被实际调用
    # _iter_chat_stream 依赖这些运行时字段；真实实例由 __init__ 设置，测试里
    # 绕过 __init__ 是为了不读 env / 不创建真实 OpenAI client，所以这里手动补齐。
    llm._stream_lock = threading.Lock()
    llm._stream_seq = 0
    llm._active_streams = {}
    llm._stream_poll_seconds = 0.02
    llm._stream_idle_log_seconds = 60.0
    llm._stream_join_seconds = 0.5
    return llm


class _BlockingStream:
    """模拟 provider 已经返回 stream，但后续 chunk 永远不来的场景。"""

    def __init__(self) -> None:
        self.iter_started = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        self.iter_started.set()
        self.closed.wait(timeout=5.0)
        return iter(())

    def close(self) -> None:
        self.closed.set()


# ========== usage 工具 ==========


class TestUsageToDict(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(_usage_to_dict(None))

    def test_object_to_dict(self):
        usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        self.assertEqual(_usage_to_dict(usage), {
            "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
        })

    def test_missing_fields_default_zero(self):
        usage = SimpleNamespace()
        self.assertEqual(_usage_to_dict(usage), {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        })

    def test_openai_cached_tokens_shape(self):
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=5,
            total_tokens=105,
            prompt_tokens_details=SimpleNamespace(cached_tokens=70),
        )
        self.assertEqual(_usage_to_dict(usage), {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "cached_prompt_tokens": 70,
            "prompt_cache_hit_tokens": 70,
            "cache_hit_rate": 0.7,
        })

    def test_siliconflow_cache_hit_miss_shape(self):
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
        }
        self.assertEqual(_usage_to_dict(usage), {
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "total_tokens": 105,
            "cached_prompt_tokens": 80,
            "prompt_cache_hit_tokens": 80,
            "prompt_cache_miss_tokens": 20,
            "cache_hit_rate": 0.8,
        })


# ========== Function Calling 流式事件 ==========


class TestThinkWithFunctionCallingEvents(unittest.TestCase):
    def setUp(self):
        self.llm = _make_llm()
        self.bus = EventBus()
        self.events = collect_all(self.bus)

    def _stream(self, chunks):
        """让 client.chat.completions.create 返回给定 chunk 序列。"""
        def fake_create(**kwargs):
            return iter(chunks)
        self.llm.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            ),
        )

    def test_text_delta_events(self):
        self._stream([
            _chunk(_delta(content="Hello")),
            _chunk(_delta(content=" world")),
            _chunk(_delta(content="!")),
        ])
        result = self.llm._think_with_Function_Calling(
            messages=[], event_bus=self.bus, round_idx=1,
        )
        self.assertEqual(result["answer"], "Hello world!")

        text_events = [e for e in self.events if isinstance(e, TextDelta)]
        self.assertEqual(len(text_events), 3)
        self.assertEqual(text_events[0].delta, "Hello")
        self.assertEqual(text_events[0].accumulated, "Hello")
        self.assertEqual(text_events[1].accumulated, "Hello world")
        self.assertEqual(text_events[2].accumulated, "Hello world!")
        # round_idx 透传
        self.assertEqual(text_events[0].round_idx, 1)

    def test_reasoning_delta_events(self):
        self._stream([
            _chunk(_delta(reasoning="Let me think")),
            _chunk(_delta(reasoning=" about it")),
            _chunk(_delta(content="Answer.")),
        ])
        result = self.llm._think_with_Function_Calling(
            messages=[], event_bus=self.bus, round_idx=2,
        )
        self.assertEqual(result["reasoning_content"], "Let me think about it")
        self.assertEqual(result["answer"], "Answer.")

        rd = [e for e in self.events if isinstance(e, ReasoningDelta)]
        self.assertEqual(len(rd), 2)
        self.assertEqual(rd[1].accumulated, "Let me think about it")

    def test_tool_call_planned_events(self):
        # tool_calls 流式分片：name 分两次 + arguments 分两次
        self._stream([
            _chunk(_delta(tool_calls=[
                _tool_call_chunk(0, id="call_1", type_="function", name="bas"),
            ])),
            _chunk(_delta(tool_calls=[
                _tool_call_chunk(0, name="h"),
            ])),
            _chunk(_delta(tool_calls=[
                _tool_call_chunk(0, args='{"command":"'),
            ])),
            _chunk(_delta(tool_calls=[
                _tool_call_chunk(0, args='ls"}'),
            ])),
        ])
        result = self.llm._think_with_Function_Calling(
            messages=[], event_bus=self.bus, round_idx=3,
        )
        self.assertEqual(len(result["tool_calls"]), 1)
        self.assertEqual(result["tool_calls"][0]["function"]["name"], "bash")
        self.assertEqual(
            result["tool_calls"][0]["function"]["arguments"], '{"command":"ls"}',
        )

        planned = [e for e in self.events if isinstance(e, ToolCallPlanned)]
        self.assertEqual(len(planned), 1)
        self.assertEqual(planned[0].name, "bash")
        self.assertEqual(planned[0].call_id, "call_1")
        self.assertEqual(planned[0].arguments_json, '{"command":"ls"}')
        self.assertEqual(planned[0].round_idx, 3)

    def test_token_usage_event_from_last_chunk(self):
        usage = SimpleNamespace(
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            prompt_cache_hit_tokens=64,
            prompt_cache_miss_tokens=36,
        )
        self._stream([
            _chunk(_delta(content="hi")),
            _chunk(delta=None, usage=usage),  # 末尾 usage chunk
        ])
        result = self.llm._think_with_Function_Calling(
            messages=[], event_bus=self.bus,
        )
        usage_events = [e for e in self.events if isinstance(e, TokenUsage)]
        self.assertEqual(len(usage_events), 1)
        self.assertEqual(usage_events[0].prompt_tokens, 100)
        self.assertEqual(usage_events[0].completion_tokens, 20)
        self.assertEqual(usage_events[0].total_tokens, 120)
        self.assertEqual(usage_events[0].cached_prompt_tokens, 64)
        self.assertEqual(usage_events[0].prompt_cache_hit_tokens, 64)
        self.assertEqual(usage_events[0].prompt_cache_miss_tokens, 36)
        self.assertEqual(usage_events[0].cache_hit_rate, 0.64)
        # 返回值里也带 usage
        self.assertEqual(result["usage"], {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "cached_prompt_tokens": 64,
            "prompt_cache_hit_tokens": 64,
            "prompt_cache_miss_tokens": 36,
            "cache_hit_rate": 0.64,
        })

    def test_no_usage_event_when_no_usage(self):
        self._stream([
            _chunk(_delta(content="hi")),
        ])
        self.llm._think_with_Function_Calling(messages=[], event_bus=self.bus)
        usage_events = [e for e in self.events if isinstance(e, TokenUsage)]
        self.assertEqual(len(usage_events), 0)

    def test_cancel_at_next_chunk_boundary(self):
        cancel_event = threading.Event()
        chunks_consumed = []

        def fake_create(**kwargs):
            def gen():
                yield _chunk(_delta(content="part1"))
                chunks_consumed.append(1)
                cancel_event.set()  # 模拟外部中断
                yield _chunk(_delta(content="part2"))  # 这块应该不被消费
                chunks_consumed.append(2)
                yield _chunk(_delta(content="part3"))
                chunks_consumed.append(3)
            return gen()
        self.llm.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=fake_create),
            ),
        )

        result = self.llm._think_with_Function_Calling(
            messages=[], event_bus=self.bus, cancel_event=cancel_event,
        )

        # part2/part3 不应被处理为 TextDelta
        text_events = [e for e in self.events if isinstance(e, TextDelta)]
        self.assertEqual([e.delta for e in text_events], ["part1"])
        # Cancelled 事件被发出
        cancelled = [e for e in self.events if isinstance(e, Cancelled)]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].where, "llm_stream")
        # 累积的 answer 只有 part1
        self.assertEqual(result["answer"], "part1")

    def test_cancel_while_waiting_for_next_chunk_closes_stream(self):
        """取消不依赖“下一个 chunk”到来，能直接关闭正在等待的 stream。"""
        cancel_event = threading.Event()
        blocking_stream = _BlockingStream()
        self.llm.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: blocking_stream),
            ),
        )
        result_box = {}

        def run_think():
            result_box["result"] = self.llm._think_with_Function_Calling(
                messages=[],
                event_bus=self.bus,
                cancel_event=cancel_event,
                round_idx=4,
            )

        thread = threading.Thread(target=run_think, daemon=True)
        thread.start()
        self.assertTrue(blocking_stream.iter_started.wait(timeout=1.0))

        cancel_event.set()
        deadline = time.time() + 1.5
        while thread.is_alive() and time.time() < deadline:
            time.sleep(0.01)

        self.assertFalse(thread.is_alive(), "think should return after cancel without a new chunk")
        self.assertTrue(blocking_stream.closed.is_set(), "active stream should be closed on cancel")
        cancelled = [e for e in self.events if isinstance(e, Cancelled)]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0].where, "llm_stream")
        self.assertEqual(result_box["result"]["answer"], "")


# ========== 默认行为（无 bus）回归 ==========


class TestThinkWithFunctionCallingDefaultBehavior(unittest.TestCase):
    """无 EventBus 时维持旧行为：直接 print 到 stdout，不抛事件。"""

    def setUp(self):
        self.llm = _make_llm()

    def test_no_bus_still_returns_correct_shape(self):
        chunks = [_chunk(_delta(content="hello"))]
        self.llm.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: iter(chunks)),
            ),
        )
        result = self.llm._think_with_Function_Calling(messages=[])
        self.assertEqual(result["answer"], "hello")
        self.assertEqual(result["tool_calls"], [])
        # usage 字段在没 usage chunk 时是 None
        self.assertIsNone(result["usage"])


# ========== 不支持 FC 的分支 ==========


class TestThinkNoFunctionCalling(unittest.TestCase):
    def setUp(self):
        self.llm = _make_llm()
        self.llm.is_Function_Calling = False
        self.bus = EventBus()
        self.events = collect_all(self.bus)

    def _stream(self, chunks):
        self.llm.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: iter(chunks)),
            ),
        )

    def test_text_deltas_emitted(self):
        self._stream([
            _chunk(_delta(content="A")),
            _chunk(_delta(content="B")),
        ])
        result = self.llm._think_no_Function_Calling(
            messages=[], event_bus=self.bus,
        )
        self.assertEqual(result, ["AB", None])
        text = [e for e in self.events if isinstance(e, TextDelta)]
        self.assertEqual([e.delta for e in text], ["A", "B"])

    def test_cancel(self):
        cancel_event = threading.Event()

        def gen():
            yield _chunk(_delta(content="X"))
            cancel_event.set()
            yield _chunk(_delta(content="Y"))
        self.llm.client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **kw: gen()),
            ),
        )
        result = self.llm._think_no_Function_Calling(
            messages=[], event_bus=self.bus, cancel_event=cancel_event,
        )
        self.assertEqual(result, ["X", None])
        cancelled = [e for e in self.events if isinstance(e, Cancelled)]
        self.assertEqual(len(cancelled), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
