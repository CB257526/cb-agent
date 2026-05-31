"""统一中断令牌

让 agent 调用链（LLM 流式 / 工具执行）共享同一份"是否应中止"信号。

设计：
- CancelToken 包装一个 threading.Event，提供 cancel() / is_cancelled() / wait()
  三个方法，跨线程使用安全
- 通过 ContextVar 暴露给当前调用栈中的工具——工具内部不需要显式接受 token
  参数，调用 get_current_cancel_token() 即可拿到
- AgentSession 在 chat() 入口创建 token 并 set 到 ContextVar；ToolExecutor
  通过 contextvars.copy_context() 把 token 传到 worker thread

使用：
    token = CancelToken()
    set_current_cancel_token(token)   # 在主线程调用
    ...
    # 在某个工具或 LLM 流式循环里：
    if get_current_cancel_token().is_cancelled():
        raise Cancelled()

或者直接传给 LLM.think(..., cancel_event=token.event)。
"""

from __future__ import annotations

import contextvars
import threading
from typing import Optional


class CancelToken:
    """轻量中断令牌，包 threading.Event。"""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def event(self) -> threading.Event:
        """暴露底层 Event，给 LLM.think(cancel_event=...) 直接用。"""
        return self._event

    def cancel(self) -> None:
        """触发中断。多次调用幂等。"""
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        """阻塞等到 cancel；timeout 秒后还没 cancel 返回 False。"""
        return self._event.wait(timeout)

    def reset(self) -> None:
        """清除 cancel 状态。一般不用，新会话直接造新 token 更清晰。"""
        self._event.clear()


# ContextVar：让工具/LLM 不显式传 token 也能拿到
_current_token: contextvars.ContextVar[Optional[CancelToken]] = (
    contextvars.ContextVar("cb_agent_cancel_token", default=None)
)


def set_current_cancel_token(token: Optional[CancelToken]) -> contextvars.Token:
    """绑到当前 context。返回的 Token 用于 reset。"""
    return _current_token.set(token)


def get_current_cancel_token() -> Optional[CancelToken]:
    """取当前 context 的 token，未绑定返回 None。"""
    return _current_token.get()


def reset_current_cancel_token(reset_token: contextvars.Token) -> None:
    """配对 set_current_cancel_token 用。"""
    _current_token.reset(reset_token)


__all__ = [
    "CancelToken",
    "set_current_cancel_token",
    "get_current_cancel_token",
    "reset_current_cancel_token",
]
