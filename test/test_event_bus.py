"""EventBus + 事件类型 单测。"""

from __future__ import annotations

import threading
import time
import unittest

from agent.event_bus import EventBus, collect_all
from agent.events import (
    BackgroundNotification, Cancelled, Done, Error, ReasoningDelta,
    RoundEnd, RoundStart, TextDelta, TokenUsage, ToolCallPlanned,
    ToolComplete, ToolStart,
)


class TestEventDataclasses(unittest.TestCase):
    def test_text_delta_defaults(self):
        ev = TextDelta(delta="hi")
        self.assertEqual(ev.delta, "hi")
        self.assertEqual(ev.accumulated, "")
        self.assertEqual(ev.round_idx, 0)
        self.assertEqual(ev.type, "text_delta")
        self.assertGreater(ev.timestamp, 0)

    def test_type_field_immutable_via_init(self):
        """type 字段是 init=False，不能在构造时传。"""
        with self.assertRaises(TypeError):
            TextDelta(delta="x", type="other")  # type: ignore[call-arg]

    def test_all_event_types_have_distinct_type_field(self):
        types = {
            TextDelta(delta="").type,
            ReasoningDelta(delta="").type,
            TokenUsage(prompt_tokens=0, completion_tokens=0, total_tokens=0).type,
            ToolCallPlanned(call_id="x", name="y", arguments_json="{}").type,
            ToolStart(call_id="x", name="y", arguments={}).type,
            ToolComplete(call_id="x", name="y", result="{}", duration_seconds=0.1).type,
            RoundStart(round_idx=1, max_rounds=8).type,
            RoundEnd(round_idx=1, has_tool_calls=False).type,
            Done(final_answer="ok", rounds_used=1).type,
            Error(where="llm", message="bad").type,
            Cancelled(where="llm_stream").type,
            BackgroundNotification(
                task_id="t1", status="done", exit_code=0, output_path="/x",
            ).type,
        }
        # 12 个事件类型，全不同
        self.assertEqual(len(types), 12)


class TestEventBusBasic(unittest.TestCase):
    def setUp(self):
        self.bus = EventBus()

    def test_subscribe_all_receives_any_event(self):
        events = collect_all(self.bus)
        self.bus.emit(TextDelta(delta="hi"))
        self.bus.emit(RoundEnd(round_idx=1, has_tool_calls=False))
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0], TextDelta)
        self.assertIsInstance(events[1], RoundEnd)

    def test_typed_subscriber_filters(self):
        text_events = []
        round_events = []
        self.bus.subscribe(text_events.append, TextDelta)
        self.bus.subscribe(round_events.append, RoundEnd)

        self.bus.emit(TextDelta(delta="a"))
        self.bus.emit(RoundEnd(round_idx=1, has_tool_calls=True))
        self.bus.emit(TextDelta(delta="b"))

        self.assertEqual(len(text_events), 2)
        self.assertEqual(len(round_events), 1)

    def test_subscriber_count(self):
        self.assertEqual(self.bus.subscriber_count, 0)
        self.bus.subscribe(lambda e: None)
        self.bus.subscribe(lambda e: None, TextDelta)
        self.assertEqual(self.bus.subscriber_count, 2)

    def test_unsubscribe(self):
        events = []
        sub = self.bus.subscribe(events.append)
        self.bus.emit(TextDelta(delta="x"))
        self.bus.unsubscribe(sub)
        self.bus.emit(TextDelta(delta="y"))
        # 第二次 emit 时 sub 已退订
        self.assertEqual(len(events), 1)

    def test_unsubscribe_unknown_silent(self):
        # 未订阅就调 unsubscribe 不应抛
        self.bus.unsubscribe(lambda e: None)

    def test_clear(self):
        self.bus.subscribe(lambda e: None)
        self.bus.subscribe(lambda e: None, TextDelta)
        self.bus.clear()
        self.assertEqual(self.bus.subscriber_count, 0)


class TestEventBusErrorIsolation(unittest.TestCase):
    def test_subscriber_exception_does_not_block_others(self):
        bus = EventBus()
        good_calls = []
        bus.subscribe(lambda e: (_ for _ in ()).throw(RuntimeError("boom")))
        bus.subscribe(good_calls.append)
        # emit 不应抛
        bus.emit(TextDelta(delta="x"))
        # 后续订阅者仍收到事件
        self.assertEqual(len(good_calls), 1)

    def test_typed_and_all_subscribers_both_called(self):
        bus = EventBus()
        all_events = []
        text_events = []
        bus.subscribe(all_events.append)
        bus.subscribe(text_events.append, TextDelta)
        bus.emit(TextDelta(delta="hi"))
        bus.emit(RoundEnd(round_idx=1, has_tool_calls=False))
        self.assertEqual(len(all_events), 2)
        self.assertEqual(len(text_events), 1)


class TestEventBusThreadSafety(unittest.TestCase):
    def test_concurrent_emit(self):
        """多线程并发 emit，订阅者应收到全部事件，无丢失、无重复。"""
        bus = EventBus()
        received = []
        lock = threading.Lock()

        def collector(ev):
            with lock:
                received.append(ev)

        bus.subscribe(collector)

        N_THREADS = 8
        N_EVENTS_PER_THREAD = 100

        def worker(tid):
            for i in range(N_EVENTS_PER_THREAD):
                bus.emit(TextDelta(delta=f"t{tid}-{i}", round_idx=tid))

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(received), N_THREADS * N_EVENTS_PER_THREAD)

    def test_subscribe_during_emit_safe(self):
        """emit 过程中订阅新订阅者不应崩。新订阅者从下一个 emit 开始收。"""
        bus = EventBus()
        late_events = []

        def first_handler(ev):
            # 第一个订阅者在收到事件时再加新订阅者
            bus.subscribe(late_events.append)

        bus.subscribe(first_handler)
        bus.emit(TextDelta(delta="a"))  # late 还没注册
        bus.emit(TextDelta(delta="b"))  # late 现在能收
        # late 至少收到 b（也可能不收 a，取决于快照时机；当前实现是不收）
        self.assertGreaterEqual(len(late_events), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
