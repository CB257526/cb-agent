"""Gateway: 把 cb-agent 的 EventBus + AgentSession 跟 stdio JSON-RPC 接起来。

两条通路：
1. agent → UI：订阅 EventBus 所有事件 → 序列化成 JSON-RPC notification → 写 stdout
2. UI → agent：阻塞读 stdin，按 method 分发：
   - prompt.submit  → 在 asyncio loop 上启动 session.chat_async（不阻塞 stdin 读循环）
   - session.cancel → 直接 set 当前 token（threading.Event.set 线程安全）
   - session.compact → 压缩当前会话上下文，保留 transcript 审计
   - session.mcp_status → 查询 MCP 后台连接进度
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
from agent.events import Event, PermissionModeChanged, PetUpdated
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
        logger.info("rpc dispatch: method=%s id=%s params_keys=%s", method, rpc_id, sorted(params.keys()) if isinstance(params, dict) else [])

        if method == "prompt.submit":
            self._handle_prompt_submit(rpc_id, params)
        elif method == "session.cancel":
            self._handle_cancel(rpc_id, params)
        elif method == "session.quit":
            self._handle_quit(rpc_id)
        elif method == "session.clear_history":
            self._handle_clear_history(rpc_id)
        elif method == "session.compact":
            self._handle_compact(rpc_id)
        elif method == "session.mcp_status":
            self._handle_mcp_status(rpc_id)
        elif method == "session.list_sessions":
            self._handle_list_sessions(rpc_id)
        elif method == "session.create":
            self._handle_create_session(rpc_id)
        elif method == "session.switch":
            self._handle_switch_session(rpc_id, params)
        elif method == "session.set_mode":
            self._handle_set_mode(rpc_id, params)
        elif method == "session.set_permission_mode":
            self._handle_set_permission_mode(rpc_id, params)
        elif method == "session.get_permission_mode":
            self._handle_get_permission_mode(rpc_id)
        elif method == "session.get_plan_state":
            self._handle_get_plan_state(rpc_id)
        elif method == "session.approve_plan":
            self._handle_approve_plan(rpc_id)
        elif method == "session.reject_plan":
            self._handle_reject_plan(rpc_id, params)
        elif method == "session.list_tools":
            self._handle_list_tools(rpc_id)
        elif method == "session.load_skill":
            self._handle_load_skill(rpc_id, params)
        elif method == "session.answer_question":
            self._handle_answer_question(rpc_id, params)
        elif method == "pet.get_state":
            self._handle_pet_get_state(rpc_id)
        elif method == "pet.command":
            self._handle_pet_command(rpc_id, params)
        else:
            logger.warning("rpc unknown method: method=%r id=%s", method, rpc_id)
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_METHOD_NOT_FOUND,
                           "message": f"unknown method: {method!r}"},
                ))

    def _handle_prompt_submit(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        text = params.get("text", "")
        attachments = params.get("attachments") or []
        if not isinstance(text, str):
            logger.warning("prompt rejected: invalid text type id=%s", rpc_id)
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_INVALID_PARAMS,
                           "message": "params.text must be string"},
                ))
            return
        if not isinstance(attachments, list) or not all(isinstance(item, dict) for item in attachments):
            logger.warning("prompt rejected: invalid attachments id=%s", rpc_id)
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_INVALID_PARAMS,
                           "message": "params.attachments must be object[]"},
                ))
            return
        if not text.strip() and not attachments:
            logger.warning("prompt rejected: empty text and attachments id=%s", rpc_id)
            if rpc_id is not None:
                self.transport.write(make_response(
                    rpc_id,
                    error={"code": _ERR_INVALID_PARAMS,
                           "message": "params.text or params.attachments required"},
                ))
            return

        # 单 session：拒绝并发 chat。UI 应该等上一个 done 事件再发下一个 prompt
        with self._busy_lock:
            if self._busy:
                logger.info("prompt rejected: busy id=%s text_chars=%s attachments=%s", rpc_id, len(text), len(attachments))
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
        logger.info("prompt accepted: id=%s text_chars=%s attachments=%s", rpc_id, len(text), len(attachments))

        # 投递到 asyncio loop
        loop = self._loop
        if loop is None:
            with self._busy_lock:
                self._busy = False
            logger.error("prompt accepted but event loop unavailable: id=%s", rpc_id)
            return
        asyncio.run_coroutine_threadsafe(self._run_chat(text, attachments), loop)

    async def _run_chat(self, text: str, attachments: Optional[list] = None) -> None:
        token = CancelToken()
        logger.info("chat task start: text_chars=%s attachments=%s", len(text), len(attachments or []))
        try:
            await self.session.chat_async(text, cancel_token=token, attachments=attachments or [])
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
            logger.info("chat task finished")

    def _handle_cancel(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        token = self.session.current_cancel_token
        if token is None:
            logger.info("cancel requested but no active token: id=%s", rpc_id)
            if rpc_id is not None:
                self.transport.write(make_response(rpc_id, result={"cancelled": False}))
            return
        token.cancel()
        logger.info("cancel requested: id=%s", rpc_id)
        closed_streams = 0
        llm = getattr(self.session, "llm", None)
        cancel_streams = getattr(llm, "cancel_active_streams", None)
        if callable(cancel_streams):
            # cancel token 只能让正在执行的 Python 逻辑“下一次检查时”退出；
            # 如果当前卡在 OpenAI SDK 的 stream 网络读上，就可能一直等不到下一次检查。
            # 主动 close 活跃 stream 可以从 RPC 线程直接打断底层响应，避免 TUI 长时间 busy。
            try:
                closed_streams = int(cancel_streams("gateway_session_cancel") or 0)
            except Exception:
                logger.exception("failed to close active LLM streams on cancel")
        if rpc_id is not None:
            self.transport.write(make_response(
                rpc_id,
                result={"cancelled": True, "closed_streams": closed_streams},
            ))
        logger.info("cancel completed: id=%s closed_streams=%s", rpc_id, closed_streams)

    def _handle_quit(self, rpc_id: Any) -> None:
        logger.info("quit requested: id=%s", rpc_id)
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

    def _handle_compact(self, rpc_id: Any) -> None:
        """压缩当前会话上下文。

        compact 会重写内存 history，并写 compact.json 快照。它和 create/switch 一样
        不能在 chat 忙碌时执行，否则当前 chat 可能一边读取旧 history，一边被另
        一个 RPC 改写并落盘，造成上下文和 transcript 的对应关系变乱。
        """
        if rpc_id is None:
            return
        if self._is_busy():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_BUSY, "message": "session busy"},
            ))
            return
        try:
            payload = self.session.compact_context()
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=payload))

    def _handle_mcp_status(self, rpc_id: Any) -> None:
        """返回 MCP 后台连接状态。

        MCP 状态和 chat history 无关，是纯运行时状态：UI 可以在 agent 忙碌时
        查询它，用来展示“哪些 server 还在连接”。如果 run_agent.py 没有给
        session 挂载 provider（例如单测或旧装配方式），这里返回 disabled，
        保持 JSON-RPC 接口稳定。
        """
        if rpc_id is None:
            return
        try:
            # 查询时顺手触发一次后台加载：正常 TUI 启动路径会在 gateway_ready 后
            # 已经触发；这个兜底主要服务 CLI/测试或未来自定义 Gateway 装配。
            starter = getattr(self.session, "mcp_background_loader", None)
            if callable(starter):
                status = starter()
            else:
                provider = getattr(self.session, "mcp_status_provider", None)
                status = provider() if callable(provider) else {
                    "status": "disabled",
                    "servers": [],
                    "total": 0,
                    "connected": 0,
                    "failed": 0,
                    "error": "MCP status provider unavailable",
                }
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=status))

    def _handle_list_sessions(self, rpc_id: Any) -> None:
        """列出项目级本地会话。

        返回值只包含轻量摘要，不包含 transcript 全文。TUI 用它渲染会话切换面板；
        真正切换时再通过 ``session.switch`` 拿对应会话的最近 history。
        """
        if rpc_id is None:
            return
        try:
            sessions = self.session.list_sessions()
            current = self.session.current_session_payload().get("session")
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(
            rpc_id,
            result={"sessions": sessions, "current": current},
        ))

    def _handle_create_session(self, rpc_id: Any) -> None:
        """新建空白会话并切换过去。

        为避免上下文串线，busy 时不允许新建/切换；否则一个正在执行的 chat 可能在
        旧会话开始，却在新会话目录落盘。
        """
        if rpc_id is None:
            return
        if self._is_busy():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_BUSY, "message": "session busy"},
            ))
            return
        try:
            payload = self.session.create_session()
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=payload))

    def _handle_switch_session(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        """切换到指定 session_id，并返回该会话恢复后的普通 history。"""
        if rpc_id is None:
            return
        if self._is_busy():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_BUSY, "message": "session busy"},
            ))
            return
        session_id = params.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "params.session_id required"},
            ))
            return
        try:
            payload = self.session.switch_session(session_id.strip())
        except ValueError as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": str(e)},
            ))
            return
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=payload))

    def _handle_set_mode(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        """RPC: session.set_mode → 切换 Plan/Execute 协作模式。

        前端通过此 RPC 请求切换协作模式（/plan 命令或 UI 按钮）。
        - "plan": LLM 只能做探索性阅读和提问，禁止修改文件
        - "execute": 正常模式，所有工具可用
        切换成功后通过 PlanModeChanged 事件广播给所有前端。
        session busy 时拒绝（与 chat 互斥）。
        """
        if rpc_id is None:
            return
        if self._is_busy():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_BUSY, "message": "session busy"},
            ))
            return
        mode = params.get("mode")
        if mode not in ("execute", "plan"):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "params.mode must be execute or plan"},
            ))
            return
        try:
            payload = self.session.set_collaboration_mode(str(mode))
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=payload))

    def _current_permission_mode(self) -> str:
        provider = getattr(self.session, "permission_mode_provider", None)
        if callable(provider):
            mode = provider()
        else:
            mode = "request_approval"
        return mode if mode in ("request_approval", "full_access") else "request_approval"

    def _handle_get_permission_mode(self, rpc_id: Any) -> None:
        if rpc_id is None:
            return
        self.transport.write(make_response(
            rpc_id,
            result={"permission_mode": self._current_permission_mode()},
        ))

    def _handle_set_permission_mode(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        if rpc_id is None:
            return
        mode = params.get("permission_mode")
        if mode not in ("request_approval", "full_access"):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "params.permission_mode must be request_approval or full_access"},
            ))
            return
        setter = getattr(self.session, "permission_mode_setter", None)
        if not callable(setter):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": "permission mode setter unavailable"},
            ))
            return
        try:
            payload = setter(str(mode))
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.event_bus.emit(PermissionModeChanged(permission_mode=payload["permission_mode"]))
        self.transport.write(make_response(rpc_id, result=payload))

    def _handle_get_plan_state(self, rpc_id: Any) -> None:
        """RPC: session.get_plan_state → 查询当前 Plan 状态。

        前端在连接恢复或模式切换后调用此 RPC 同步 plan 面板状态。
        返回当前 plan 模式、pending/approved 计划内容和状态信息。
        不检查 busy，允许在 chat 进行中查询。
        """
        if rpc_id is None:
            return
        try:
            state = self.session.plan_state()
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result={"plan_state": state}))

    def _handle_approve_plan(self, rpc_id: Any) -> None:
        """RPC: session.approve_plan → 用户批准 pending plan。

        将 current.md 复制为 approved.md，状态切为 approved，
        mode 切回 execute，approved_plan 内容注入后续 LLM 上下文。
        通过 PlanApproved + PlanModeChanged 事件广播。
        要求存在 pending plan（current.md），否则返回 ValueError。
        """
        if rpc_id is None:
            return
        if self._is_busy():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_BUSY, "message": "session busy"},
            ))
            return
        try:
            payload = self.session.approve_plan()
        except ValueError as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": str(e)},
            ))
            return
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=payload))

    def _handle_reject_plan(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        """RPC: session.reject_plan → 用户拒绝 pending plan 并附反馈。

        feedback 文本持久化到 state.json，下一轮 chat 注入 LLM 上下文
        告知模型"用户拒绝了上一个计划，请根据反馈修改"。
        mode 保持在 plan，status 切为 rejected。
        通过 PlanRejected + PlanModeChanged 事件广播。
        """
        if rpc_id is None:
            return
        if self._is_busy():
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_BUSY, "message": "session busy"},
            ))
            return
        feedback = params.get("feedback", "")
        if not isinstance(feedback, str):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "params.feedback must be string"},
            ))
            return
        try:
            payload = self.session.reject_plan(feedback)
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=payload))

    def _handle_list_tools(self, rpc_id: Any) -> None:
        """返回当前 registry 注册的工具列表，供 UI 端 / 命令展示。

        结果形状: { tools: [{name, description, schema?}] }
        - schema 直接透传 OpenAI function-calling 格式（含 parameters），UI 自己决定怎么展示
        - 失败时仍走 RPC error；不会影响 chat 主流程
        """
        if rpc_id is None:
            return
        try:
            registry = self.session.registry
            schemas = registry.get_tools_description_openai_schema() or []
            tools = []
            for entry in schemas:
                fn = entry.get("function") if isinstance(entry, dict) else None
                if not fn:
                    continue
                tools.append({
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "schema": fn.get("parameters"),
                })
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result={"tools": tools}))

    def _handle_load_skill(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        """用户显式加载 Skill，供 TUI /skill 命令使用。"""
        if rpc_id is None:
            return
        skill_name = params.get("name")
        args = params.get("args", "")
        if not isinstance(args, str):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "params.args must be string"},
            ))
            return

        manager = getattr(self.session, "skill_manager", None)
        if manager is None:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": "skill manager unavailable"},
            ))
            return

        try:
            manager.check_for_changes()
            if not isinstance(skill_name, str) or not skill_name.strip():
                self.transport.write(make_response(
                    rpc_id,
                    result={"name": None, "content": manager.format_skill_list()},
                ))
                return
            skill = manager.get_skill(skill_name.strip())
            if skill is None:
                available = [s.name for s in manager.list_skills()]
                self.transport.write(make_response(
                    rpc_id,
                    error={
                        "code": _ERR_INVALID_PARAMS,
                        "message": f"未找到 Skill '{skill_name.strip()}'。可用 Skill: {', '.join(available)}",
                    },
                ))
                return
            if not skill.user_invocable:
                self.transport.write(make_response(
                    rpc_id,
                    error={
                        "code": _ERR_INVALID_PARAMS,
                        "message": f"Skill '{skill.name}' 不允许用户通过 slash 命令手动触发",
                    },
                ))
                return
            manager.record_usage(skill.name)
            content = manager.load_skill_content(skill.name, args)
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return

        self.transport.write(make_response(
            rpc_id,
            result={"name": skill.name, "content": content},
        ))

    def _handle_answer_question(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        """UI 端用户在 AskQuestionPanel 选完后回灌答案。

        params 形状:
          { question_id: str,
            selected_labels: [str, ...],   # 单选给一个；多选给多个
            other_text?: str,              # 选了 "Other" 时填的自定义文本
            cancelled?: bool }             # 用户主动取消 → True

        registry.submit_answer 唤醒工具线程。问题不存在（超时/重复回灌）走 result.delivered=False，
        不返回错误：UI 收到 ack 即可，业务态由后续 ask_user_question_answered 事件给出。
        """
        if rpc_id is None:
            return
        qid = params.get("question_id")
        labels = params.get("selected_labels") or []
        other_text = params.get("other_text")
        cancelled = bool(params.get("cancelled", False))

        if not isinstance(qid, str) or not qid:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "question_id required"},
            ))
            return
        if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "selected_labels must be string[]"},
            ))
            return

        delivered = self.session.question_registry.submit_answer(
            qid,
            selected_labels=labels,
            other_text=other_text if isinstance(other_text, str) else None,
            cancelled=cancelled,
        )
        self.transport.write(make_response(rpc_id, result={"delivered": delivered}))

    def _pet_unavailable_state(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "status": "unavailable",
            "visible": False,
            "activity": "idle",
            "current_pet_id": None,
            "current_pet": None,
            "pets": [],
            "runtime": {
                "kind": "cbagent-python",
                "running": False,
                "path": None,
            },
            "message": "Pet manager unavailable",
        }

    def _handle_pet_get_state(self, rpc_id: Any) -> None:
        if rpc_id is None:
            return
        manager = getattr(self.session, "pet_manager", None)
        if manager is None:
            self.transport.write(make_response(rpc_id, result=self._pet_unavailable_state()))
            return
        try:
            state = manager.state()
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=state))

    def _handle_pet_command(self, rpc_id: Any, params: Dict[str, Any]) -> None:
        if rpc_id is None:
            return
        args = params.get("args", "")
        if not isinstance(args, str):
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INVALID_PARAMS, "message": "params.args must be string"},
            ))
            return
        manager = getattr(self.session, "pet_manager", None)
        if manager is None:
            self.transport.write(make_response(
                rpc_id,
                result={
                    "text": "Pet manager unavailable",
                    "changed": False,
                    "state": self._pet_unavailable_state(),
                },
            ))
            return
        try:
            result = manager.handle_command(args)
            if result.get("changed"):
                self.event_bus.emit(PetUpdated(state=result.get("state") or manager.state(), reason="command"))
        except Exception as e:
            self.transport.write(make_response(
                rpc_id,
                error={"code": _ERR_INTERNAL, "message": str(e)},
            ))
            return
        self.transport.write(make_response(rpc_id, result=result))

    def _is_busy(self) -> bool:
        """线程安全读取 busy 状态，供会话切换/新建等同步 RPC 使用。"""
        with self._busy_lock:
            return self._busy

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
        logger.info("gateway serve start")

        # stdin 读线程：每读到一行 dispatch 一次。线程内不持有 loop 引用做 await，
        # 所有跨线程的事都用 run_coroutine_threadsafe / call_soon_threadsafe。
        reader_thread = threading.Thread(
            target=self._reader_loop,
            name="cb-agent-stdin",
            daemon=True,  # 主流程退出时不挂着
        )
        reader_thread.start()

        # 发一条 ready 事件，告诉 UI 可以开始交互了
        session_payload = self.session.current_session_payload()
        self.transport.write({
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "gateway_ready",
                "model": getattr(self.session.llm, "model", "unknown"),
                "session": session_payload.get("session"),
                "history": session_payload.get("history", []),
                "context_window": session_payload.get("context_window"),
                "plan_state": session_payload.get("plan_state"),
                "permission_mode": self._current_permission_mode(),
            },
        })
        logger.info(
            "gateway ready emitted: model=%s history=%s",
            getattr(self.session.llm, "model", "unknown"),
            len(session_payload.get("history", [])),
        )

        # ready 事件发出后再启动 MCP 后台连接。这样 TUI 可以先渲染首屏和输入框，
        # 后续 mcp_status 事件会自然追加/更新状态；配置再多也不会卡住用户第一句。
        starter = getattr(self.session, "mcp_background_loader", None)
        if callable(starter):
            try:
                starter()
            except Exception:
                logger.exception("failed to start MCP background loading")

        await self._stop_event.wait()
        logger.info("gateway serve stop requested")

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
