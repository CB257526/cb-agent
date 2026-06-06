"""线程安全事件总线

负责把 LLMStream / ToolExecutor / AgentSession 发出的事件分发到所有订阅者
（CLIRenderer / TextualApp / FastAPI handler / 测试钩子等）。

设计契约：
- **同步发布**：emit(event) 在调用线程同步遍历所有订阅者。订阅者要做"重活"
  自己异步化，绝不能阻塞 emit。
- **错误隔离**：单个订阅者抛异常不影响其它订阅者，吞掉但记 logger.exception。
- **遍历快照**：emit 时拿订阅者列表快照遍历，避免遍历中订阅者列表被改导致
  RuntimeError。
- **保序**：同类型事件按 emit 调用顺序到达每个订阅者；跨类型不保证（无谓
  开销，订阅者按需自己拼时间线）。
- **不缓存**：订阅者必须在事件 emit 之前订阅。后来订阅者拿不到历史事件——
  agent 会话是 live stream，不是 event store。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional, Type

from .events import Event

logger = logging.getLogger(__name__)


# 订阅者签名：接受一个事件，返回值忽略
Subscriber = Callable[[Event], None] #输入为event的方法


class EventBus:
    """进程内事件总线。一个 AgentSession 通常持有一个 bus 实例。

    线程安全：
    - 订阅者列表用锁保护
    - emit 时复制快照后释放锁，再调订阅者，避免订阅者长时间持锁阻塞 publishers
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 全部事件的订阅者
        self._all_subscribers: List[Subscriber] = []
        # 按事件类型订阅（type[Event] -> [subscribers]）
        self._typed_subscribers: dict[Type[Event], List[Subscriber]] = {}

    # ---------- 订阅 / 取消订阅 ----------
    """
    可以理解为订阅者是一个入参为event的方法吗，假如发布者发布了一个event，
    然后就会根据这个event的类型，去对应列表（self._typed_subscribers）找这些订阅者(输入event的方法),
    然后一个个调用这些方法(订阅者)将这个event输入进去
    """

    def subscribe(
        self,
        subscriber: Subscriber,
        event_type: Optional[Type[Event]] = None,
    ) -> Subscriber:
        """订阅事件。

        Args:
            subscriber: 收到事件时的回调
            event_type: 指定类型则只收该类型；None 则收全部
        Returns:
            原订阅者（用于后续 unsubscribe）
        """
        with self._lock:
            if event_type is None:
                self._all_subscribers.append(subscriber)
            else:
                self._typed_subscribers.setdefault(event_type, []).append(subscriber)
        return subscriber

    def unsubscribe(
        self,
        subscriber: Subscriber,
        event_type: Optional[Type[Event]] = None,
    ) -> None:
        """取消订阅。subscriber 不在列表里就静默跳过。"""
        with self._lock:
            if event_type is None:
                if subscriber in self._all_subscribers:
                    self._all_subscribers.remove(subscriber)
            else:
                lst = self._typed_subscribers.get(event_type, [])
                if subscriber in lst:
                    lst.remove(subscriber)

    def clear(self) -> None:
        """清空所有订阅。测试用。"""
        with self._lock:
            self._all_subscribers.clear()
            self._typed_subscribers.clear()

    # ---------- 发布 ----------

    def emit(self, event: Event) -> None:
        """同步发布事件给所有相关订阅者。

        - 先快照订阅者列表再释放锁，遍历时即使订阅者列表被改也不影响
        - 单订阅者异常不传播
        """
        with self._lock:
            all_subs = list(self._all_subscribers) # 使用订阅者列表的快照，仿照在循环遍历过程中有订阅者使用unsubscribe导致列表被改变，从而会因锁而抛出RuntimeError
            typed_subs = list(self._typed_subscribers.get(type(event), []))

        for sub in all_subs:
            self._safe_call(sub, event) # 调用订阅者，将event输入进去
        for sub in typed_subs:
            self._safe_call(sub, event)

    @staticmethod # 静态方法，不需要实例化,直接调用即可
    def _safe_call(sub: Subscriber, event: Event) -> None:
        try:
            sub(event)
        except Exception:  # noqa: BLE001
            logger.exception(
                "事件订阅者抛异常: subscriber=%r event_type=%s",
                sub, type(event).__name__,
            )

    # ---------- 调试辅助 ----------

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            n = len(self._all_subscribers)
            for lst in self._typed_subscribers.values():
                n += len(lst)
            return n


# ========== 内置便捷订阅者 ==========


def collect_all(bus: EventBus) -> List[Event]:
    """订阅所有事件并返回累积列表。仅测试用，生产别用（无大小限制）。

    用法：
        events = collect_all(bus)
        # ... 触发 emit ...
        assert isinstance(events[0], TextDelta)
    """
    collected: List[Event] = []
    bus.subscribe(collected.append)
    return collected


__all__ = ["EventBus", "Subscriber", "collect_all"]
