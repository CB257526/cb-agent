"""NapCat / OneBot V11 反向 WebSocket 适配器。"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import requests
from websockets.asyncio.server import ServerConnection, serve

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
from agent.qq.config import QQConfig
from agent.qq.file_delivery import FileDeliveryError, QQFileDeliveryManager
from agent.qq.onebot import outbound_segment_to_onebot, parse_onebot_event, parse_onebot_message_event
from agent.session import AgentSession

logger = logging.getLogger(__name__)
_RESOURCE_SEGMENT_KINDS = {"file", "image", "sticker", "audio", "video"}


@dataclass
class _ConversationQueueState:
    """单个 QQ 会话的串行队列状态。

    这里刻意只缓存轻量的 ``asyncio.Lock`` 和等待数量，不缓存 AgentSession。
    每条消息真正开始处理时都会向 Runner 要一个新的 AgentSession 对象；私聊由
    该对象从磁盘恢复该好友历史，群聊则使用无持久化的临时会话。
    """

    lock: asyncio.Lock
    pending: int = 0


class QQNapCatAdapter:
    """QQ/NapCat OneBot V11 反向 WebSocket 服务。

    NapCat 作为客户端连到本服务；本服务通过同一条 WebSocket 接收事件并发送 action。
    每个 QQ 群聊或私聊会通过 ``session_factory`` 拿到独立 AgentSession；同一会话
    内串行，不同会话之间可以并发运行。
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        event_bus: EventBus,
        config: Optional[QQConfig] = None,
        session_factory: Optional[Callable[[ConversationKey], AgentSession]] = None,
    ) -> None:
        self.session = session
        self._session_factory = session_factory or (lambda _conversation: session)
        self.event_bus = event_bus
        self.config = config or QQConfig.from_env()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connection: Optional[ServerConnection] = None
        self._connection_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending_actions: Dict[str, asyncio.Future] = {}
        self._conversation_queues: Dict[str, _ConversationQueueState] = {}
        self._conversation_queues_lock = asyncio.Lock()
        self._attachment_dir = _resolve_attachment_dir()
        self._file_delivery = QQFileDeliveryManager(self.config)
        self._renderer = PlatformEventRenderer(
            event_bus=event_bus,
            send=self._enqueue_outbound,
        )

    def serve_forever(self) -> None:
        asyncio.run(self.serve())

    async def serve(self) -> None:
        if not self.config.enabled:
            logger.warning("QQ transport disabled by QQ_ENABLE=0")
            return
        self._loop = asyncio.get_running_loop()
        logger.info("QQ/NapCat reverse websocket serving on %s:%s", self.config.host, self.config.port)
        async with serve(
            self._handle_connection,
            self.config.host,
            self.config.port,
            max_size=16 * 1024 * 1024,
        ):
            await asyncio.Future()

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        if not self._authorize(websocket):
            await websocket.close(code=1008, reason="invalid access token")
            return
        async with self._connection_lock:
            if self._connection is not None and self._connection is not websocket:
                logger.warning("QQ/NapCat new connection replaced existing connection")
                try:
                    await self._connection.close(code=1012, reason="replaced")
                except Exception:
                    pass
            self._connection = websocket
        logger.info("QQ/NapCat connected: remote=%s", getattr(websocket, "remote_address", None))
        try:
            async for raw in websocket:
                await self._handle_raw_message(raw)
        finally:
            async with self._connection_lock:
                if self._connection is websocket:
                    self._connection = None
            logger.info("QQ/NapCat disconnected")

    def _authorize(self, websocket: ServerConnection) -> bool:
        token = self.config.access_token
        if not token:
            return True
        request = getattr(websocket, "request", None)
        headers = getattr(request, "headers", {}) or {}
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth == f"Bearer {token}" or auth == token:
            return True
        path = str(getattr(request, "path", "") or "")
        return f"access_token={token}" in path

    async def _handle_raw_message(self, raw: Any) -> None:
        try:
            payload = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8"))
        except Exception:
            logger.warning("QQ/NapCat received invalid json: %r", raw)
            return
        if not isinstance(payload, dict):
            return

        echo = payload.get("echo")
        if echo is not None and ("status" in payload or "retcode" in payload or "data" in payload):
            self._resolve_action(str(echo), payload)
            return

        # 有待回答问题时，用户不需要再次 @ 机器人；因此先用不要求唤醒的解析路径。
        inbound_for_question = parse_onebot_message_event(payload, self.config, require_wakeup=False)
        if inbound_for_question is not None and self._renderer.has_pending_question(inbound_for_question.conversation):
            consumed = self._renderer.try_answer_pending(
                conversation=inbound_for_question.conversation,
                text=inbound_for_question.text,
                registry=self.session.question_registry,
                sender_id=inbound_for_question.sender_id,
            )
            if consumed:
                return

        inbound = parse_onebot_event(payload, self.config, require_wakeup=True)
        if inbound is None:
            return
        # 不在 WebSocket 消息循环里等待完整 agent 回复。这样 A 群正在长任务时，B 群
        # 的消息仍能被解析并启动自己的会话；同一会话的忙碌拦截在 _start_agent_run。
        asyncio.create_task(self._run_inbound(inbound))

    def _resolve_action(self, echo: str, payload: Dict[str, Any]) -> None:
        fut = self._pending_actions.pop(echo, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    async def _run_inbound(self, inbound: InboundMessage) -> None:
        await self._enrich_inbound_message(inbound)
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
            )
        except Exception as exc:
            logger.exception("QQ agent run failed")
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

    async def _acquire_conversation_queue(
        self,
        conversation: ConversationKey,
    ) -> _ConversationQueueState:
        """获取当前会话的串行执行权。

        同一个好友/群聊内部按消息到达顺序排队，避免两条消息同时写同一份私聊
        transcript 或同时等待同一个 ask_user_question。不同 ConversationKey 拿到
        不同锁，仍可并发运行，不会被某个长任务全局堵住。
        """

        async with self._conversation_queues_lock:
            key = conversation.stable_id
            state = self._conversation_queues.get(key)
            if state is None:
                state = _ConversationQueueState(lock=asyncio.Lock())
                self._conversation_queues[key] = state
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
            # 没有等待者时移除轻量队列状态，避免长期积累大量一次性好友/群聊 key。
            if state.pending == 0 and not state.lock.locked():
                self._conversation_queues.pop(conversation.stable_id, None)

    async def _enrich_inbound_message(self, inbound: InboundMessage) -> None:
        """补全 NapCat 入站消息里需要 action 查询的信息。

        OneBot 消息段有时只给 ``file_id`` 或引用消息 ID，不直接给 URL/正文。这里参考
        AstrBot 的处理方式：文件走 get_group_file_url/get_private_file_url，引用消息
        走 get_msg。补全失败只写日志，不阻断用户这轮文本消息。
        """

        await self._resolve_inbound_file_urls(inbound)
        await self._append_reply_message_summary(inbound)

    async def _resolve_inbound_file_urls(self, inbound: InboundMessage) -> None:
        for item in inbound.attachments:
            if item.url or not item.file_id:
                continue
            if item.modality != "file":
                continue
            action = "get_group_file_url" if inbound.conversation.kind == "group" else "get_private_file_url"
            params: Dict[str, Any] = {"file_id": item.file_id}
            if inbound.conversation.kind == "group":
                params["group_id"] = _maybe_int(inbound.conversation.id)
            try:
                result = await self.call_action(action, params)
                data = _action_data(result)
                url = str(data.get("url") or result.get("url") or "").strip()
                if not url:
                    logger.warning("QQ file url action returned no url: action=%s result=%s", action, result)
                    continue
                item.url = url
                file_name = str(data.get("file_name") or data.get("name") or item.file_name or "").strip()
                if file_name:
                    item.file_name = file_name
                item.description = f"QQ 文件 {item.file_name or item.file_id} URL={url}"
            except Exception:
                logger.exception("QQ file url resolve failed: conversation=%s file_id=%s", inbound.conversation.stable_id, item.file_id)

    async def _append_reply_message_summary(self, inbound: InboundMessage) -> None:
        if not inbound.reply_to_message_id:
            return
        try:
            result = await self.call_action(
                "get_msg",
                {"message_id": _maybe_int(inbound.reply_to_message_id)},
            )
            data = _action_data(result)
            if not data:
                return
            payload = dict(data)
            payload.setdefault("post_type", "message")
            payload.setdefault("message_type", "group" if inbound.conversation.kind == "group" else "private")
            if inbound.conversation.kind == "group":
                payload.setdefault("group_id", _maybe_int(inbound.conversation.id))
            else:
                payload.setdefault("user_id", _maybe_int(inbound.sender_id or inbound.conversation.id))
            quoted = parse_onebot_message_event(payload, self.config, require_wakeup=False)
            if quoted is None:
                return
            parts = [
                f"[引用消息详情 message_id={inbound.reply_to_message_id} sender={quoted.sender_name or quoted.sender_id}]",
                quoted.text.strip(),
            ]
            for item in quoted.attachments:
                desc = item.description or item.url or item.file_name
                if desc:
                    parts.append(f"[引用附件] {item.modality}: {desc}")
            quote_text = "\n".join(p for p in parts if p).strip()
            if quote_text:
                inbound.text = f"{quote_text}\n\n{inbound.text}".strip()
        except Exception:
            logger.exception("QQ reply message resolve failed: message_id=%s", inbound.reply_to_message_id)

    async def _materialize_inbound_attachments(self, inbound: InboundMessage) -> None:
        """尽量把 QQ 图片/音频 URL 下载成本地附件。

        现有多模态输入层只接受本地路径；QQ/NapCat 通常会给图片 URL。这里做一层
        平台适配：能下载就写到 ``.cbagent/platform_attachments/qq``，失败则保留
        URL 描述，让模型至少知道用户发过附件，但不会假装已经看到了内容。
        """
        for item in inbound.attachments:
            if item.path or not item.url or item.modality not in {"image", "audio"}:
                continue
            try:
                item.path = await asyncio.to_thread(
                    self._download_attachment,
                    item.url,
                    item.file_name,
                    item.modality,
                )
            except Exception as exc:
                logger.warning(
                    "QQ attachment download failed: url=%s file=%s error=%s",
                    item.url,
                    item.file_name,
                    exc,
                )

    def _download_attachment(self, url: str, file_name: str, modality: str) -> str:
        limit_mb = _float_env("CBAGENT_ATTACHMENT_MAX_MB", 20.0)
        limit = max(1, int(limit_mb * 1024 * 1024))
        response = requests.get(url, timeout=20, stream=True)
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length and int(content_length) > limit:
            raise ValueError(f"attachment exceeds limit: {content_length} > {limit}")

        suffix = Path(file_name or "").suffix
        if not suffix:
            suffix = mimetypes.guess_extension(response.headers.get("content-type", "").split(";")[0].strip()) or ""
        if not suffix:
            suffix = ".bin"
        safe_name = _safe_file_stem(file_name or f"{modality}-{uuid.uuid4().hex[:8]}")
        target = self._attachment_dir / f"{safe_name}-{uuid.uuid4().hex[:8]}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)

        total = 0
        with target.open("wb") as fh:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > limit:
                    try:
                        target.unlink(missing_ok=True)
                    except Exception:
                        pass
                    raise ValueError(f"attachment exceeds limit: {total} > {limit}")
                fh.write(chunk)
        return str(target.resolve())

    def _enqueue_outbound(self, message: OutboundMessage) -> None:
        loop = self._loop
        if loop is None:
            logger.warning("QQ outbound dropped before event loop ready: reason=%s", message.reason)
            return
        loop.call_soon_threadsafe(lambda: asyncio.create_task(self.send_outbound(message)))

    async def send_outbound(self, message: OutboundMessage) -> None:
        for segment in message.segments:
            if segment.kind in _RESOURCE_SEGMENT_KINDS:
                ok, details = await self._send_resource_segment(message.conversation, segment)
                if ok:
                    continue
                await self._send_text(
                    message.conversation,
                    _format_resource_send_failure(segment, details),
                )
                continue
            onebot_segments = outbound_segment_to_onebot(segment)
            if onebot_segments:
                await self._send_message_segments(message.conversation, onebot_segments)

    async def _send_text(self, conversation: ConversationKey, text: str) -> None:
        await self._send_message_segments(conversation, [{"type": "text", "data": {"text": text}}])

    async def _send_message_segments(self, conversation: ConversationKey, segments: list[dict[str, Any]]) -> Dict[str, Any]:
        action = "send_group_msg" if conversation.kind == "group" else "send_private_msg"
        params: Dict[str, Any] = {"message": segments}
        if conversation.kind == "group":
            params["group_id"] = int(conversation.id) if str(conversation.id).isdigit() else conversation.id
        else:
            params["user_id"] = int(conversation.id) if str(conversation.id).isdigit() else conversation.id
        return await self.call_action(action, params)

    async def _send_resource_segment(
        self,
        conversation: ConversationKey,
        segment: OutboundSegment,
    ) -> tuple[bool, list[str]]:
        """按配置把本地资源交付给 NapCat。

        旧实现只有“直接传宿主机路径”一种方式。这里先把本地文件转换成一个或多个
        NapCat 可读候选引用，再逐个尝试。Docker 部署时可以用 mapped_path/http，
        本机部署仍默认走 path，不破坏原有行为。
        """

        delivery = getattr(self, "_file_delivery", None)
        if delivery is None:
            # 兼容部分单测里没有调用 __init__ 的 DummyAdapter。
            delivery = QQFileDeliveryManager(getattr(self, "config", QQConfig()))
            self._file_delivery = delivery
        try:
            plan = await asyncio.to_thread(delivery.build_plan, segment.path)
        except FileDeliveryError as exc:
            logger.warning("QQ resource delivery plan failed: kind=%s path=%s error=%s", segment.kind, segment.path, exc)
            return False, [str(exc)]

        details = list(plan.errors)
        if not plan.candidates:
            return False, details or ["没有可用的文件交付方式"]

        for candidate in plan.candidates:
            # candidate.ref 可能是 URL、base64:// 或 Docker 容器内 POSIX 路径。
            # 这里不能走 OutboundSegment.file_segment()，因为它会用 Path() 规范化，
            # 在 Windows 宿主机上会把 /app/outbound/a.txt 改成 \app\outbound\a.txt。
            routed = OutboundSegment(
                kind=segment.kind,
                path=candidate.ref,
                file_name=segment.file_name or Path(candidate.source_path).name,
                text=segment.text,
                metadata={
                    **segment.metadata,
                    "delivery_method": candidate.method,
                    "delivery_note": candidate.note,
                    "source_path": candidate.source_path,
                },
            )
            try:
                if segment.kind == "file":
                    ok = await self._send_file_segment(conversation, routed)
                else:
                    ok = await self._send_media_segment(conversation, routed)
            except Exception as exc:
                ok = False
                logger.exception(
                    "QQ resource send candidate failed: method=%s kind=%s path=%s",
                    candidate.method,
                    segment.kind,
                    segment.path,
                )
                details.append(f"{candidate.method}: {type(exc).__name__}: {exc}")
            if ok:
                logger.info(
                    "QQ resource sent: kind=%s method=%s source=%s ref=%s",
                    segment.kind,
                    candidate.method,
                    candidate.source_path,
                    _clip_log_ref(candidate.ref),
                )
                return True, details
            details.append(f"{candidate.method}: NapCat action 返回失败")
        return False, details

    async def _send_media_segment(self, conversation: ConversationKey, segment: OutboundSegment) -> bool:
        onebot_segments = outbound_segment_to_onebot(segment)
        if not onebot_segments:
            return False
        result = await self._send_message_segments(conversation, onebot_segments)
        return _action_ok(result)

    async def _send_file_segment(self, conversation: ConversationKey, segment: OutboundSegment) -> bool:
        params: Dict[str, Any]
        if conversation.kind == "group":
            action = "upload_group_file"
            params = {
                "group_id": int(conversation.id) if str(conversation.id).isdigit() else conversation.id,
                "file": segment.path,
                "name": segment.file_name or segment.path,
            }
        else:
            action = "upload_private_file"
            params = {
                "user_id": int(conversation.id) if str(conversation.id).isdigit() else conversation.id,
                "file": segment.path,
                "name": segment.file_name or segment.path,
            }
        try:
            result = await self.call_action(action, params)
            return _action_ok(result)
        except Exception:
            logger.exception("QQ file upload action failed: action=%s path=%s", action, segment.path)
            return False

    async def call_action(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        connection = self._connection
        if connection is None:
            raise RuntimeError("NapCat websocket is not connected")
        echo = f"cb_{uuid.uuid4().hex}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_actions[echo] = fut
        payload = {"action": action, "params": params, "echo": echo}
        async with self._send_lock:
            await connection.send(json.dumps(payload, ensure_ascii=False))
        try:
            result = await asyncio.wait_for(fut, timeout=self.config.action_timeout_seconds)
        except Exception:
            self._pending_actions.pop(echo, None)
            raise
        if not _action_ok(result):
            logger.warning("QQ action returned non-ok: action=%s result=%s", action, result)
        return result


def _action_ok(result: Dict[str, Any]) -> bool:
    status = str(result.get("status") or "").lower()
    retcode = result.get("retcode")
    if status:
        return status in {"ok", "async"}
    if retcode is not None:
        return retcode in {0, "0"}
    return True


def _action_data(result: Dict[str, Any]) -> Dict[str, Any]:
    """从 OneBot action 响应中取出 data 对象。

    NapCat/OneBot action 有的把业务字段放在 ``data``，有的旧实现直接平铺在顶层。
    调用方先读 data，再按需回退顶层，可以兼容这两类返回格式。
    """

    data = result.get("data")
    if isinstance(data, dict):
        return data
    return {
        k: v
        for k, v in result.items()
        if k not in {"status", "retcode", "echo", "wording"}
    }


def _format_resource_send_failure(segment: OutboundSegment, details: list[str]) -> str:
    """生成通讯软件端可读的资源发送失败提示。"""

    name = segment.file_name or Path(str(segment.path or "")).name or "未命名文件"
    lines = [
        f"文件发送失败，已降级为路径提示：{name}",
        str(segment.path or ""),
    ]
    clean_details = [item for item in details if item]
    if clean_details:
        lines.append("")
        lines.append("失败原因：")
        for item in clean_details[:4]:
            lines.append(f"- {item}")
        if len(clean_details) > 4:
            lines.append(f"- ... 还有 {len(clean_details) - 4} 条")
    lines.append("")
    lines.append("如果 NapCat 在 Docker 中，请配置 QQ_FILE_DELIVERY_MODE=mapped_path 或 http。")
    return "\n".join(lines).strip()


def _clip_log_ref(value: str, limit: int = 220) -> str:
    """日志里折叠长 URL/base64，避免把大文件内容刷进日志。"""

    text = str(value or "")
    if text.startswith("base64://"):
        return f"base64://...({len(text)} chars)"
    if len(text) <= limit:
        return text
    return text[:limit] + "...(truncated)"


def _maybe_int(value: Any) -> Any:
    """QQ 号、群号、message_id 能转数字就转，不能转时保留原值。"""

    text = str(value or "")
    return int(text) if text.isdigit() else value


def _resolve_attachment_dir() -> Path:
    raw = os.getenv("CBAGENT_PLATFORM_ATTACHMENT_DIR") or ".cbagent/platform_attachments/qq"
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve()


def _safe_file_stem(value: str) -> str:
    stem = Path(value).stem or "attachment"
    return re.sub(r"[^0-9A-Za-z._-]+", "_", stem)[:80] or "attachment"


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except Exception:
        return default


__all__ = ["QQNapCatAdapter"]
