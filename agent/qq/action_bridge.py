"""QQ / NapCat action 共享调用桥。

QQ 适配器运行在 asyncio WebSocket 事件循环里，而模型工具通常在
``AgentSession.chat_async`` 的 worker 线程中执行。这个模块用一个很小的全局桥
把两边连接起来：适配器连接成功后注册自己的 ``call_action`` 协程，工具线程通过
``asyncio.run_coroutine_threadsafe`` 把 action 投递回同一个事件循环。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional


ActionCaller = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class QQActionBridge:
    """保存当前 QQ adapter 的 action 调用入口。

    这里不直接保存 WebSocket 对象，也不重复实现 echo / timeout / action ok 判断；
    adapter 仍然是唯一真正懂 OneBot WebSocket 协议的地方。桥只负责跨线程调度，
    因此未来如果 adapter 内部换实现，qqtool 不需要跟着改。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._caller: Optional[ActionCaller] = None
        self._token: Optional[str] = None

    def register(self, loop: asyncio.AbstractEventLoop, caller: ActionCaller) -> str:
        """注册当前可用的 QQ action 调用器。"""

        token = f"qq_action_{uuid.uuid4().hex}"
        with self._lock:
            self._loop = loop
            self._caller = caller
            self._token = token
        return token

    def unregister(self, token: str) -> None:
        """在 adapter 退出时注销调用器，避免工具继续投递到失效连接。"""

        with self._lock:
            if self._token == token:
                self._loop = None
                self._caller = None
                self._token = None

    def is_ready(self) -> bool:
        """返回当前是否已有可投递的 adapter action 通道。"""

        with self._lock:
            return self._loop is not None and self._caller is not None

    def call(self, action: str, params: Dict[str, Any], *, timeout: Optional[float] = None) -> Dict[str, Any]:
        """从同步工具线程调用 NapCat action。

        如果调用方本身已经在 adapter 的事件循环里，说明当前代码路径不适合使用同步
        ``qqtool``；为了避免死锁，这里直接给出明确错误。
        """

        with self._lock:
            loop = self._loop
            caller = self._caller
        if loop is None or caller is None:
            raise RuntimeError("NapCat websocket is not connected")
        if not loop.is_running():
            raise RuntimeError("NapCat websocket event loop is not running")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("QQ action bridge cannot be called synchronously from the QQ event loop")

        future = asyncio.run_coroutine_threadsafe(caller(action, params), loop)
        return future.result(timeout=timeout)


global_qq_action_bridge = QQActionBridge()


__all__ = ["QQActionBridge", "global_qq_action_bridge"]
