"""微信 OC 长轮询适配器。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from agent.cancel import CancelToken
from agent.event_bus import EventBus
from agent.platforms.context import (
    reset_current_platform_conversation,
    reset_current_platform_sender,
    set_current_platform_conversation,
    set_current_platform_sender,
)
from agent.platforms.messages import ConversationKey, InboundMessage, OutboundMessage, OutboundSegment
from agent.platforms.renderer import PlatformEventRenderer
from agent.wechat.action_bridge import global_wechat_action_bridge
from agent.wechat.client import WeChatOCClient
from agent.wechat.config import WeChatConfig
from agent.wechat.media import materialize_inbound_attachment
from agent.wechat.oc_types import (
    build_media_send_body,
    build_text_send_body,
    item_kind_for_path,
    parse_wechat_message,
)

if TYPE_CHECKING:
    from agent.session import AgentSession

logger = logging.getLogger(__name__)
_RESOURCE_SEGMENT_KINDS = {"file", "image", "sticker", "audio", "video"}


@dataclass
class _ConversationQueueState:
    lock: asyncio.Lock
    pending: int = 0


class WeChatOCAdapter:
    """个人微信 OC transport。

    与 QQ/NapCat 不同，微信 OC 没有本地反向 WebSocket。这里由 cb-agent 主动：
    扫码拿 token -> HTTP 长轮询 getupdates -> HTTP sendmessage/CDN 上传回复。
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        event_bus: EventBus,
        config: Optional[WeChatConfig] = None,
        session_factory: Optional[Callable[[ConversationKey], AgentSession]] = None,
    ) -> None:
        self.session = session
        self._session_factory = session_factory or (lambda _conversation: session)
        self.event_bus = event_bus
        self.config = config or WeChatConfig.from_env()
        self.client = WeChatOCClient(self.config)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._conversation_queues: Dict[str, _ConversationQueueState] = {}
        self._conversation_queues_lock = asyncio.Lock()
        self._renderer = PlatformEventRenderer(event_bus=event_bus, send=self._enqueue_outbound)
        self._sync_buf = ""
        self._context_tokens: Dict[str, str] = {}
        self._token = self.config.token
        self._account_id = self.config.account_id
        self._running = False

    def serve_forever(self) -> None:
        asyncio.run(self.serve())

    async def serve(self) -> None:
        if not self.config.enabled:
            logger.warning("WeChat transport disabled by WECHAT_ENABLE=0")
            return
        self._loop = asyncio.get_running_loop()
        self._load_state()
        await self._ensure_login()
        bridge_token = global_wechat_action_bridge.register(self._loop, self.call_action)
        self._running = True
        logger.info("WeChat OC transport started: account_id=%s base_url=%s", self._account_id, self.client.base_url)
        try:
            await self._poll_loop()
        finally:
            self._running = False
            global_wechat_action_bridge.unregister(bridge_token)

    async def _ensure_login(self) -> None:
        if self._token:
            self.client.update_auth(token=self._token)
            return

        logger.info("WeChat OC token missing, starting QR login")
        qrcode = await self._refresh_login_qrcode()
        deadline = time.monotonic() + 480
        pending_verify_code = ""
        scanned_reported = False
        refresh_count = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(self.config.qr_poll_interval_seconds)
            result = await asyncio.to_thread(self.client.poll_login, qrcode, pending_verify_code)
            status = str(result.get("status") or "wait").strip()
            logger.info("WeChat QR login status=%s", status)
            if status == "wait":
                continue
            if status == "scaned":
                if pending_verify_code:
                    logger.info("WeChat QR verify code accepted, resume polling")
                    pending_verify_code = ""
                if not scanned_reported:
                    print("[微信登录] 已扫描，正在等待手机端确认...")
                    scanned_reported = True
                continue
            if status == "scaned_but_redirect":
                redirect_host = str(result.get("redirect_host") or "").strip()
                if redirect_host:
                    base_url = _redirect_base_url(redirect_host)
                    self.client.update_auth(base_url=base_url)
                    logger.info("WeChat QR login redirected to %s", base_url)
                else:
                    logger.warning("WeChat QR login returned scaned_but_redirect without redirect_host")
                continue
            if status == "need_verifycode":
                pending_verify_code = await asyncio.to_thread(
                    _read_verify_code_from_stdin,
                    "[微信登录] 手机端要求配对码，请输入手机微信显示的数字后回车：",
                )
                continue
            if status == "verify_code_blocked":
                pending_verify_code = ""
                refresh_count += 1
                if refresh_count > 3:
                    raise RuntimeError("微信扫码登录配对码多次错误，已停止登录流程")
                print(f"[微信登录] 配对码错误次数过多，正在刷新二维码({refresh_count}/3)...")
                qrcode = await self._refresh_login_qrcode()
                scanned_reported = False
                continue
            if status == "binded_redirect":
                raise RuntimeError(
                    "微信账号已绑定到当前 OC 服务，但本地没有可用 token。"
                    "请恢复 .cbagent/wechat/state.json，或在 .env 配置 WECHAT_TOKEN/WECHAT_ACCOUNT_ID 后重启。"
                )
            if status == "confirmed":
                token = str(result.get("bot_token") or "").strip()
                if not token:
                    raise RuntimeError("微信登录成功但没有返回 bot_token")
                self._token = token
                self._account_id = str(result.get("ilink_bot_id") or self._account_id or "").strip()
                base_url = str(result.get("baseurl") or "").strip()
                self.client.update_auth(token=self._token, base_url=base_url)
                self._save_state()
                return
            if status in {"expired", "cancel", "canceled", "denied"}:
                if status == "expired":
                    refresh_count += 1
                    if refresh_count <= 3:
                        print(f"[微信登录] 二维码已过期，正在刷新({refresh_count}/3)...")
                        qrcode = await self._refresh_login_qrcode()
                        scanned_reported = False
                        continue
                raise RuntimeError(f"微信扫码登录未完成：{status}")
            logger.warning("WeChat QR login returned unknown status=%s payload=%s", status, result)
        raise RuntimeError("微信扫码登录超时")

    async def _refresh_login_qrcode(self) -> str:
        """重新获取并打印登录二维码，返回后续轮询需要的 qrcode token。"""

        start = await asyncio.to_thread(self.client.start_login)
        qrcode = str(start.get("qrcode") or "").strip()
        qrcode_url = str(start.get("qrcode_img_content") or start.get("qrcodeUrl") or "").strip()
        if not qrcode or not qrcode_url:
            raise RuntimeError(f"微信二维码响应异常：{start}")
        _print_qr_login(qrcode_url)
        return qrcode

    async def _poll_loop(self) -> None:
        failures = 0
        while True:
            try:
                response = await asyncio.to_thread(self.client.get_updates, self._sync_buf)
                failures = 0
                if _api_failed(response):
                    if response.get("errcode") == -14 or response.get("ret") == -14:
                        logger.warning("WeChat session expired, clearing token and restarting login")
                        self._token = ""
                        self.client.update_auth(token="")
                        self._save_state()
                        await self._ensure_login()
                    else:
                        logger.warning("WeChat getupdates failed: %s", response)
                        await asyncio.sleep(2)
                    continue
                if response.get("get_updates_buf"):
                    self._sync_buf = str(response.get("get_updates_buf") or "")
                    self._save_state()
                for item in response.get("msgs") or []:
                    if isinstance(item, dict):
                        await self._handle_wechat_message(item)
            except Exception:
                failures += 1
                logger.exception("WeChat poll loop error")
                await asyncio.sleep(30 if failures >= 3 else 2)
                if failures >= 3:
                    failures = 0

    async def _handle_wechat_message(self, payload: Dict[str, Any]) -> None:
        self._remember_context_token(payload)

        inbound_for_question = parse_wechat_message(payload, self.config, require_wakeup=False)
        if inbound_for_question is not None and self._renderer.has_pending_question(inbound_for_question.conversation):
            consumed = self._renderer.try_answer_pending(
                conversation=inbound_for_question.conversation,
                text=inbound_for_question.text,
                registry=self.session.question_registry,
                sender_id=inbound_for_question.sender_id,
            )
            if consumed:
                return

        inbound = parse_wechat_message(payload, self.config, require_wakeup=True)
        if inbound is None:
            return
        asyncio.create_task(self._run_inbound(inbound))

    async def _run_inbound(self, inbound: InboundMessage) -> None:
        await self._materialize_inbound_attachments(inbound)
        await self._start_agent_run(inbound)

    async def _start_agent_run(self, inbound: InboundMessage) -> None:
        queue_state = await self._acquire_conversation_queue(inbound.conversation)
        token = CancelToken()
        context_token = set_current_platform_conversation(inbound.conversation)
        sender_token = set_current_platform_sender(inbound.sender_id)
        self._renderer.begin_run(inbound.conversation, sender_id=inbound.sender_id)
        try:
            session = self._session_factory(inbound.conversation)
            await session.chat_async(
                inbound.prompt_text(),
                cancel_token=token,
                attachments=inbound.prompt_attachments(),
                persistent_user_text=inbound.persistent_text(),
            )
        except Exception as exc:
            logger.exception("WeChat agent run failed")
            await self.send_outbound(OutboundMessage.text(
                inbound.conversation,
                f"处理消息时发生异常：{type(exc).__name__}: {exc}",
                reason="error",
                kind="status",
            ))
        finally:
            self._renderer.end_run(inbound.conversation)
            reset_current_platform_conversation(context_token)
            reset_current_platform_sender(sender_token)
            await self._release_conversation_queue(inbound.conversation, queue_state)

    async def _acquire_conversation_queue(self, conversation: ConversationKey) -> _ConversationQueueState:
        async with self._conversation_queues_lock:
            state = self._conversation_queues.get(conversation.stable_id)
            if state is None:
                state = _ConversationQueueState(lock=asyncio.Lock())
                self._conversation_queues[conversation.stable_id] = state
            state.pending += 1
        await state.lock.acquire()
        return state

    async def _release_conversation_queue(
        self,
        conversation: ConversationKey,
        state: _ConversationQueueState,
    ) -> None:
        state.lock.release()
        async with self._conversation_queues_lock:
            state.pending = max(0, state.pending - 1)
            if state.pending == 0 and not state.lock.locked():
                self._conversation_queues.pop(conversation.stable_id, None)

    async def _materialize_inbound_attachments(self, inbound: InboundMessage) -> None:
        for item in inbound.attachments:
            try:
                await asyncio.to_thread(
                    materialize_inbound_attachment,
                    item,
                    client=self.client,
                    config=self.config,
                )
            except Exception as exc:
                logger.warning("WeChat attachment materialize failed: file=%s error=%s", item.file_name, exc)

    def _enqueue_outbound(self, message: OutboundMessage) -> None:
        loop = self._loop
        if loop is None:
            logger.warning("WeChat outbound dropped before event loop ready: reason=%s", message.reason)
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self.send_outbound(message)))

    async def send_outbound(self, message: OutboundMessage) -> None:
        for segment in message.segments:
            if segment.kind in _RESOURCE_SEGMENT_KINDS:
                ok, detail = await self._send_resource_segment(message.conversation, segment)
                if not ok:
                    await self._send_text(message.conversation, f"文件发送失败：{detail}")
                continue
            if segment.text:
                await self._send_text(message.conversation, segment.text)

    async def _send_text(self, conversation: ConversationKey, text: str) -> Dict[str, Any]:
        target = self._recipient_for_conversation(conversation)
        body = build_text_send_body(
            to_user_id=target,
            text=text,
            context_token=self._context_token_for(conversation),
        )
        return await asyncio.to_thread(self.client.send_message, body)

    async def _send_resource_segment(self, conversation: ConversationKey, segment: OutboundSegment) -> tuple[bool, str]:
        target = self._recipient_for_conversation(conversation)
        try:
            upload_type, item_type = item_kind_for_path(segment.path, segment.kind)
            item = await asyncio.to_thread(
                self.client.prepare_media_item,
                to_user_id=target,
                file_path=segment.path,
                upload_media_type=upload_type,
                item_type=item_type,
                file_name=segment.file_name or Path(segment.path).name,
            )
            if segment.text.strip():
                await self._send_text(conversation, segment.text.strip())
            body = build_media_send_body(
                to_user_id=target,
                item=item,
                context_token=self._context_token_for(conversation),
            )
            result = await asyncio.to_thread(self.client.send_message, body)
            if _api_failed(result):
                return False, str(result)
            return True, "ok"
        except Exception as exc:
            logger.exception("WeChat resource send failed: kind=%s path=%s", segment.kind, segment.path)
            return False, f"{type(exc).__name__}: {exc}"

    async def call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = params or {}
        if action == "__cbagent_wechat_send_text__":
            conversation = self._conversation_from_params(params)
            result = await self._send_text(conversation, str(params.get("text") or params.get("message") or ""))
            return _ok({"result": result})
        if action == "__cbagent_wechat_send_media__":
            conversation = self._conversation_from_params(params)
            segment = OutboundSegment.file_segment(
                kind=str(params.get("kind") or "file"),
                path=str(params.get("path") or params.get("file") or ""),
                file_name=str(params.get("file_name") or params.get("name") or ""),
                text=str(params.get("caption") or ""),
            )
            ok, detail = await self._send_resource_segment(conversation, segment)
            return _ok({"detail": detail}) if ok else _err(detail)
        if action == "__cbagent_wechat_send_typing__":
            conversation = self._conversation_from_params(params)
            target = self._recipient_for_conversation(conversation)
            context_token = self._context_token_for(conversation)
            config = await asyncio.to_thread(self.client.get_config, user_id=target, context_token=context_token)
            ticket = str(config.get("typing_ticket") or "").strip()
            if not ticket:
                return _err(f"未获取到 typing_ticket: {config}")
            result = await asyncio.to_thread(
                self.client.send_typing,
                user_id=target,
                typing_ticket=ticket,
                cancel=bool(params.get("cancel")),
            )
            return _ok({"result": result})
        if action == "__cbagent_wechat_get_status__":
            return _ok({
                "running": self._running,
                "account_id": self._account_id,
                "has_token": bool(self._token),
                "context_tokens": len(self._context_tokens),
            })
        if action == "__cbagent_wechat_get_login_info__":
            return _ok({
                "account_id": self._account_id,
                "base_url": self.client.base_url,
                "cdn_base_url": self.client.cdn_base_url,
                "has_token": bool(self._token),
                "token_preview": (self._token[:6] + "...") if self._token else "",
            })
        return _err(f"未知微信 action: {action}")

    def _conversation_from_params(self, params: Dict[str, Any]) -> ConversationKey:
        group_id = str(params.get("group_id") or "").strip()
        user_id = str(params.get("user_id") or params.get("to_user_id") or "").strip()
        if group_id:
            raise ValueError("微信 OC 当前是当前账号的私聊 bot，不支持 group_id/群聊操作")
        if user_id:
            return ConversationKey("wechat", "private", user_id)
        raise ValueError("缺少 user_id")

    def _recipient_for_conversation(self, conversation: ConversationKey) -> str:
        return str(conversation.id)

    def _context_token_for(self, conversation: ConversationKey) -> str:
        return self._context_tokens.get(conversation.stable_id, "")

    def _remember_context_token(self, payload: Dict[str, Any]) -> None:
        context_token = str(payload.get("context_token") or "").strip()
        if not context_token:
            return
        parsed = parse_wechat_message(payload, self.config, require_wakeup=False)
        if parsed is None:
            return
        self._context_tokens[parsed.conversation.stable_id] = context_token
        self._save_state()

    def _load_state(self) -> None:
        path = self._state_file()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not self._token:
                    self._token = str(data.get("token") or "").strip()
                if not self._account_id:
                    self._account_id = str(data.get("account_id") or "").strip()
                self._sync_buf = str(data.get("sync_buf") or "")
                tokens = data.get("context_tokens")
                if isinstance(tokens, dict):
                    self._context_tokens = {str(k): str(v) for k, v in tokens.items() if str(v)}
                base_url = str(data.get("base_url") or "").strip()
                cdn_base_url = str(data.get("cdn_base_url") or "").strip()
                self.client.update_auth(token=self._token, base_url=base_url, cdn_base_url=cdn_base_url)
            except Exception:
                logger.exception("WeChat state load failed: %s", path)
        self.client.update_auth(token=self._token)

    def _save_state(self) -> None:
        path = self._state_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "token": self._token,
            "account_id": self._account_id,
            "base_url": self.client.base_url,
            "cdn_base_url": self.client.cdn_base_url,
            "sync_buf": self._sync_buf,
            "context_tokens": self._context_tokens,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            path.chmod(0o600)
        except Exception:
            pass

    def _state_file(self) -> Path:
        path = self.config.state_file.expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        return path.resolve()


def _api_failed(payload: Dict[str, Any]) -> bool:
    ret = payload.get("ret")
    err = payload.get("errcode")
    return (ret is not None and ret not in {0, "0"}) or (err is not None and err not in {0, "0"})


def _ok(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"ok": True, "data": data}


def _err(error: str) -> Dict[str, Any]:
    return {"ok": False, "error": error}


def _print_qr_login(qrcode_url: str) -> None:
    """在终端展示微信登录二维码。

    ``qrcode`` 是轻量依赖；如果用户还没安装或运行环境不支持终端二维码，就退回
    打印原始链接。微信 OC 登录本质上只需要这个 URL，二维码只是更舒服的展示层。
    """

    print("\n[微信登录] 请用手机微信扫描以下二维码并确认授权：")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(qrcode_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        print("当前环境无法渲染终端二维码，请打开下面的链接继续：")
    print(qrcode_url)


def _redirect_base_url(redirect_host: str) -> str:
    """把 OC 返回的 redirect_host 规范成完整 base_url。"""

    host = str(redirect_host or "").strip().rstrip("/")
    if host.startswith(("http://", "https://")):
        return host
    return f"https://{host}"


def _read_verify_code_from_stdin(prompt: str) -> str:
    """读取微信扫码流程里的配对码。

    OC 在部分登录场景会要求输入手机端显示的数字。这里用 stdin 读取，避免引入
    额外 UI 协议；如果服务以 systemd/headless 方式运行且没有 stdin，会给出明确错误。
    """

    try:
        return input(prompt).strip()
    except EOFError as exc:
        raise RuntimeError("当前进程没有可用 stdin，无法完成微信配对码登录") from exc


__all__ = ["WeChatOCAdapter"]
