"""统一的回合取消上下文。

取消不仅是一位布尔值。工具运行时需要知道取消原因、发生时间和剩余截止时间，
还需要在取消发生的线程中立即收到通知，才能关闭网络请求或终止子进程。
"""

from __future__ import annotations

import contextvars
import threading
import time
from enum import Enum
from typing import Callable, Dict, Optional


class CancellationReason(str, Enum):
    """取消原因。字符串枚举便于直接写入 JSON 日志。"""

    USER_CANCELLED = "user_cancelled"
    TOOL_TIMEOUT = "tool_timeout"
    SESSION_SHUTDOWN = "session_shutdown"
    PARENT_CANCELLED = "parent_cancelled"


class ToolCancelledError(RuntimeError):
    """工具在可控边界观察到取消时抛出的结构化异常。"""

    def __init__(
        self,
        reason: CancellationReason,
        message: str = "工具执行已取消",
        *,
        partial_output: str = "",
        effect_state: str = "unknown",
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.partial_output = partial_output
        self.effect_state = effect_state


class CancellationContext:
    """线程安全的取消上下文，支持父子传播和运行时回调。"""

    def __init__(
        self,
        *,
        deadline: Optional[float] = None,
        parent: Optional["CancellationContext"] = None,
    ) -> None:
        self._event = threading.Event()
        self._lock = threading.RLock()
        self._reason: Optional[CancellationReason] = None
        self._cancelled_at: Optional[float] = None
        self._deadline = deadline
        self._callbacks: Dict[int, Callable[[CancellationReason], None]] = {}
        self._next_callback_id = 1
        self._parent_unsubscribe: Optional[Callable[[], None]] = None
        if parent is not None:
            # 父回合取消时，子工具必须使用父级真实原因，不能误报成工具超时。
            self._parent_unsubscribe = parent.add_cancel_callback(self.cancel)
            if parent.is_cancelled():
                self.cancel(parent.reason or CancellationReason.PARENT_CANCELLED)

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def reason(self) -> Optional[CancellationReason]:
        with self._lock:
            return self._reason

    @property
    def cancelled_at(self) -> Optional[float]:
        with self._lock:
            return self._cancelled_at

    @property
    def deadline(self) -> Optional[float]:
        return self._deadline

    def cancel(
        self,
        reason: CancellationReason | str = CancellationReason.USER_CANCELLED,
    ) -> bool:
        """幂等触发取消；仅第一次调用负责通知运行时。"""

        normalized = (
            reason if isinstance(reason, CancellationReason)
            else CancellationReason(str(reason))
        )
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = normalized
            self._cancelled_at = time.time()
            self._event.set()
            callbacks = list(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback(normalized)
            except Exception:
                # 取消路径不能因为某个运行时清理失败而阻断其他清理回调。
                continue
        return True

    def is_cancelled(self) -> bool:
        if self._event.is_set():
            return True
        if self._deadline is not None and time.monotonic() >= self._deadline:
            self.cancel(CancellationReason.TOOL_TIMEOUT)
            return True
        return False

    def wait(self, timeout: Optional[float] = None) -> bool:
        if self.is_cancelled():
            return True
        wait_for = timeout
        remaining = self.remaining_seconds()
        if remaining is not None:
            wait_for = remaining if wait_for is None else min(wait_for, remaining)
        triggered = self._event.wait(wait_for)
        return triggered or self.is_cancelled()

    def remaining_seconds(self) -> Optional[float]:
        if self._deadline is None:
            return None
        return max(0.0, self._deadline - time.monotonic())

    def throw_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise ToolCancelledError(
                self.reason or CancellationReason.USER_CANCELLED
            )

    def child(self, *, deadline: Optional[float] = None) -> "CancellationContext":
        return CancellationContext(deadline=deadline, parent=self)

    def add_cancel_callback(
        self, callback: Callable[[CancellationReason], None]
    ) -> Callable[[], None]:
        """注册立即取消回调，并返回可幂等调用的注销函数。"""

        with self._lock:
            if self._event.is_set():
                reason = self._reason or CancellationReason.USER_CANCELLED
                callback_id = 0
            else:
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback
                reason = None
        if reason is not None:
            callback(reason)

        def unsubscribe() -> None:
            if callback_id:
                with self._lock:
                    self._callbacks.pop(callback_id, None)

        return unsubscribe

    def close(self) -> None:
        """解除父级订阅，避免完成工具长期滞留在父回调表中。"""

        unsubscribe = self._parent_unsubscribe
        self._parent_unsubscribe = None
        if unsubscribe is not None:
            unsubscribe()

    def reset(self) -> None:
        """仅为旧调用方兼容保留；新回合应创建新的上下文。"""

        with self._lock:
            self._reason = None
            self._cancelled_at = None
            self._event.clear()


# 保留旧名称，现有调用方会自动获得新的取消能力。
CancelToken = CancellationContext

_current_token: contextvars.ContextVar[Optional[CancellationContext]] = (
    contextvars.ContextVar("cb_agent_cancel_token", default=None)
)


def set_current_cancel_token(
    token: Optional[CancellationContext],
) -> contextvars.Token:
    return _current_token.set(token)


def get_current_cancel_token() -> Optional[CancellationContext]:
    return _current_token.get()


def reset_current_cancel_token(reset_token: contextvars.Token) -> None:
    _current_token.reset(reset_token)


__all__ = [
    "CancellationContext",
    "CancellationReason",
    "CancelToken",
    "ToolCancelledError",
    "set_current_cancel_token",
    "get_current_cancel_token",
    "reset_current_cancel_token",
]
