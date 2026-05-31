"""Gateway: 把 cb-agent 的 EventBus + AgentSession 跟 stdio JSON-RPC 接起来。

两条通路：
1. agent → UI：订阅 EventBus 所有事件 → 序列化成 JSON-RPC notification → 写 stdout
2. UI → agent：阻塞读 stdin，按 method 分发：
   - prompt.submit  → 在 asyncio loop 上启动 session.chat_async（不阻塞 stdin 读循环）
   - session.cancel → 直接 set 当前 token（threading.Event.set 线程安全）
   - session.quit   → 关 loop，主流程退出

线程关系：
+----------------------+        +---------------------+        +-----------+
| asyncio loop (main)  | <----- | stdin reader thread | <----- | UI stdin  |
|  - chat_async        |  call  |  - read_loop()      |  read  |           |
|  - schedule via      |  via   |  - dispatch RPC     |        |           |
|    call_soon_thread  |  fut   |                     |        |           |
+----------------------+        +---------------------+        +-----------+
        |
        | bus.emit (在工具线程 / chat 线程 / 主线程都可能)
        v
+----------------------+        +-----------+
| StdioTransport.write | -----> | UI stdout |
|  (lock 保护)         |        |           |
+----------------------+        +-----------+

stdout 重定向：
agent 内部 print/traceback 会污染 JSON 协议，构造 Gateway 时把 sys.stdout
切到 sys.stderr。这条是从 Hermes 抄的——他们也是 server.py 启动时 sys.stdout
= sys.stderr。
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from typing import Any, Dict, Optional, TextIO

from agent.cancel import CancelToken
from agent.event_bus import EventBus
from agent.events import Event
from agent.session import AgentSession
from agent.transport.jsonrpc import StdioTransport, make_event_message, make_response

logger = logging.getLogger(__name__)


# JSON-RPC 错误码（前三个是标准的，后面是业务自定义）
_ERR_PARSE = -32700
_ERR_INVALID_REQ = -32600
_ERR_METHOD_NOT_FOUND = -32601
_ERR_INVALID_PARAMS = -32602
_ERR_INTERNAL = -32603
_ERR_BUSY = -32001  # session 正忙


class Gateway:
    """连接 EventBus / AgentSession 到 stdio JSON-RPC 的协调器。

    用法（run_agent.py）：
        gw = Gateway(session=runner.session, event_bus=runner.event_bus)
        gw.serve_forever()  # 阻塞，直到 stdin EOF 或收到 session.quit
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        event_bus: EventBus,
        transport: Optional[StdioTransport] = None,
        redirect_stdout_to_stderr: bool = True,
    ) -> None:
        # 必须先做 stdout 重定向，再造 transport——否则 transport 抓到的 stdout
        # 已经是被替换后的 stderr 了
        if redirect_stdout_to_stderr:
            self._real_stdout: TextIO = sys.stdout
            sys.stdout = sys.stderr
        else:
            self._real_stdout = sys.stdout

        self.transport = transport if transport is not None else StdioTransport(
            stdin=sys.stdin,
            stdout=self._real_stdout,
        )
        self.session = session
        self.event_bus = event_bus

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._chat_task: Optional[asyncio.Task] = None
        self._busy = False  # 同一时间只允许一个 chat
        self._busy_lock = threading.Lock()

        # 订阅所有事件 → 写 transport
        # subscribe 不传 type 表示订阅全部
        self.event_bus.subscribe(self._on_event)

    # ---------- agent → UI ----------

    def _on_event(self, event: Event) -> None:
        msg = make_event_message(event)
        ok = self.transport.write(msg)
        if not ok:
            # peer 关了，没办法挽救；后续事件继续 write 也都会立即失败
            logger.warning("transport closed while emitting %s", getattr(event, "type", "?"))

    # ---------- UI → agent ----------

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """stdin 读线程里跑。RPC 同步路径走完就返回，慢操作（chat）投递到 asyncio loop。"""
        method = msg.get("method")
        rpc_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "prompt.submit":
            self._handle_prompt_submit(rpc_id, params)
        elif method == "session.cancel":
            self._handle_cancel(rpc_id, params)
        elif method == "session.quit":
            self._handle_quit(rpc_id)
        elif method == "session.clear_history":
            self._handle_clear_history(rpc_id)
        else:
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_METHOD_NOT_FOUND,
                           "message": f"unknown method: {method!r}"},
                ))

    def _handle_prompt_submit(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        text = params.get("text")
        if not isinstance(text, str) or not text.strip():
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_INVALID_PARAMS,
                           "message": "params.text must be non-empty string"},
                ))
            return

        # 单 session：拒绝并发 chat。UI 应该等上一个 done 事件再发下一个 prompt
        with self._busy_lock:
            if self._busy:
                if rpc_id is not None:
                    self.transport.write(make_response(
                        rpc_id,
                        error={"code": _ERR_BUSY, "message": "session busy"},
                    ))
                return
            self._busy = True

        # 立刻 ack——chat 是异步任务，结果通过事件流送
        if rpc_id is not None:
            self.transport.write(make_response(rpc_id, result={"status": "accepted"}))

        # 投递到 asyncio loop
        loop = self._loop
        if loop is None:
            with self._busy_lock:
                self._busy = False
            return
        asyncio.run_coroutine_threadsafe(self._run_chat(text), loop)

    async def _run_chat(self, text: str) -> None:
        token = CancelToken()
        try:
            await self.session.chat_async(text, cancel_token=token)
        except Exception as e:
            logger.exception("chat_async failed")
            # 兜底发一条 error 事件给 UI（normally 已经由 session 内部发过了）
            self.transport.write({
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "error",
                    "where": "gateway",
                    "message": str(e),
                    "exception_type": type(e).__name__,
                    "round_idx": 0,
                },
            })
        finally:
            with self._busy_lock:
                self._busy = False

    def _handle_cancel(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        token = self.session.current_cancel_token
        if token is None:
            if rpc_id is not None:
                self.transport.write(make_response(rpc_id, result={"cancelled": False}))
            return
        token.cancel()
        if rpc_id is not None:
            self.transport.write(make_response(rpc_id, result={"cancelled": True}))

    def _handle_quit(self, rpc_id: Any) -> None:
        if rpc_id is not None:
            self.transport.write(make_response(rpc_id, result={"bye": True}))
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None:
            loop.call_soon_threadsafe(stop_event.set)

    def _handle_clear_history(self, rpc_id: Any) -> None:
        try:
            self.session.clear_history()
        except Exception as e:
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_INTERNAL, "message": str(e)},
                ))
            return
        if rpc_id is not None:
            self.transport.write(make_response(rpc_id, result={"cleared": True}))

    # ---------- 启动 ----------

    def serve_forever(self) -> None:
        """阻塞主线程：起 asyncio loop + stdin 读线程，直到任意一边结束。"""
        try:
            asyncio.run(self._serve_async())
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.event_bus.unsubscribe(self._on_event)
            except Exception:
                pass

    async def _serve_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        # stdin 读线程：每读到一行 dispatch 一次。线程内不持有 loop 引用做 await，
        # 所有跨线程的事都用 run_coroutine_threadsafe / call_soon_threadsafe。
        reader_thread = threading.Thread(
            target=self._reader_loop,
            name="cb-agent-stdin",
            daemon=True,  # 主流程退出时不挂着
        )
        reader_thread.start()

        # 发一条 ready 事件，告诉 UI 可以开始交互了
        self.transport.write({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway_ready",
                "model": getattr(self.session.llm, "model", "unknown"),
            },
        })

        await self._stop_event.wait()

    def _reader_loop(self) -> None:
        """stdin 读循环。EOF 或 transport 关闭后通知 loop 停。"""
        try:
            for msg in self.transport.read_loop():
                try:
                    self._dispatch(msg)
                except Exception:
                    logger.exception("dispatch failed for %r", msg.get("method"))
        finally:
            # stdin EOF：让 loop 停
            loop = self._loop
            stop_event = self._stop_event
            if loop is not None and stop_event is not None:
                loop.call_soon_threadsafe(stop_event.set)
