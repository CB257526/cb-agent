"""微信 OC action 共享调用桥。

微信适配器运行在 asyncio 长轮询事件循环里，而模型工具通常在线程池里同步执行。
这个桥和 QQActionBridge 的职责一致：只做跨线程投递，不让 wechattool 直接持有
adapter 对象，也不把 HTTP 协议细节泄露给工具层。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional


ActionCaller = Callable[[str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


class WeChatActionBridge:
    """保存当前微信 adapter 的 action 调用入口。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._caller: Optional[ActionCaller] = None
        self._token: Optional[str] = None

    def register(self, loop: asyncio.AbstractEventLoop, caller: ActionCaller) -> str:
        token = f"wechat_action_{uuid.uuid4().hex}"
        with self._lock:
            self._loop = loop
            self._caller = caller
            self._token = token
        return token

    def unregister(self, token: str) -> None:
        with self._lock:
            if self._token == token:
                self._loop = None
                self._caller = None
                self._token = None

    def is_ready(self) -> bool:
        with self._lock:
            return self._loop is not None and self._caller is not None

    def call(self, action: str, params: Dict[str, Any], *, timeout: Optional[float] = None) -> Dict[str, Any]:
        with self._lock:
            loop = self._loop
            caller = self._caller
        if loop is None or caller is None:
            raise RuntimeError("WeChat OC transport is not running")
        if not loop.is_running():
            raise RuntimeError("WeChat OC event loop is not running")
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError("wechat action bridge cannot be called synchronously from the WeChat event loop")

        future = asyncio.run_coroutine_threadsafe(caller(action, params), loop)
        return future.result(timeout=timeout)


global_wechat_action_bridge = WeChatActionBridge()


__all__ = ["WeChatActionBridge", "global_wechat_action_bridge"]
