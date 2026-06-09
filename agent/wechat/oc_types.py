"""微信 OC 消息结构解析与出站 item 构造。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.platforms.messages import ConversationKey, InboundAttachment, InboundMessage
from agent.wechat.config import WeChatConfig


MESSAGE_TYPE_BOT = 2
MESSAGE_STATE_FINISH = 2

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

UPLOAD_IMAGE = 1
UPLOAD_VIDEO = 2
UPLOAD_FILE = 3
UPLOAD_VOICE = 4


def parse_wechat_message(
    msg: Dict[str, Any],
    config: WeChatConfig,
    *,
    require_wakeup: bool = True,
) -> Optional[InboundMessage]:
    """把微信 OC getupdates 消息转换为平台无关入站消息。

    openclaw-weixin 的 OC bot 是当前微信账号下的 direct 私聊 bot，不是一个独立的
    机器人账号。因此微信 transport 只处理私聊消息：如果上游将来下发 ``group_id``，
    这里会直接忽略，避免在微信群里误触发 agent。
    """

    if not isinstance(msg, dict):
        return None
    sender_id = str(msg.get("from_user_id") or "").strip()
    if not sender_id:
        return None

    group_id = str(msg.get("group_id") or "").strip()
    session_id = str(msg.get("session_id") or "").strip()
    if group_id:
        return None

    text, attachments, reply_to = _parse_item_list(msg.get("item_list"))

    conversation = ConversationKey(
        platform="wechat",
        kind="private",
        id=sender_id,
    )
    raw = dict(msg)
    if msg.get("context_token"):
        raw["context_token"] = msg.get("context_token")
    if session_id:
        raw["session_id"] = session_id

    return InboundMessage(
        conversation=conversation,
        sender_id=sender_id,
        sender_name=sender_id,
        text=text.strip(),
        raw=raw,
        attachments=attachments,
        message_id=str(msg.get("message_id") or msg.get("client_id") or "") or None,
        reply_to_message_id=reply_to,
    )


def build_text_send_body(*, to_user_id: str, text: str, context_token: str = "") -> Dict[str, Any]:
    """构造微信 sendmessage 文本请求体。"""

    item_list = [{"type": ITEM_TEXT, "text_item": {"text": text}}] if text else []
    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": uuid.uuid4().hex,
        "message_type": MESSAGE_TYPE_BOT,
        "message_state": MESSAGE_STATE_FINISH,
    }
    if context_token:
        msg["context_token"] = context_token
    if item_list:
        msg["item_list"] = item_list
    return {
        "msg": msg
    }


def build_single_item_send_body(
    *,
    to_user_id: str,
    item: Dict[str, Any],
    context_token: str = "",
) -> Dict[str, Any]:
    """构造只包含一个 item 的微信 sendmessage 请求体。

    openclaw-weixin 对媒体发送采用“caption 单独文本请求 + 每个媒体 item 单独请求”
    的方式。这里提供专用构造函数，避免把文本和媒体塞进同一个 item_list 后遇到
    OC 服务端兼容问题。
    """

    msg = {
        "from_user_id": "",
        "to_user_id": to_user_id,
        "client_id": uuid.uuid4().hex,
        "message_type": MESSAGE_TYPE_BOT,
        "message_state": MESSAGE_STATE_FINISH,
        "item_list": [item],
    }
    if context_token:
        msg["context_token"] = context_token
    return {
        "msg": msg
    }


def build_media_send_body(
    *,
    to_user_id: str,
    item: Dict[str, Any],
    context_token: str = "",
    caption: str = "",
) -> Dict[str, Any]:
    """构造微信 sendmessage 媒体请求体。

    兼容旧调用名；caption 不再混入同一个 item_list，调用方应先单独发送文本。
    """

    return build_single_item_send_body(
        to_user_id=to_user_id,
        item=item,
        context_token=context_token,
    )


def item_kind_for_path(path: str | Path, requested: str = "") -> Tuple[int, int]:
    """根据扩展名返回 (upload_media_type, message_item_type)。"""

    ext = Path(path).suffix.lower()
    requested = str(requested or "").strip().lower()
    if requested in {"image", "sticker"} or ext in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}:
        return UPLOAD_IMAGE, ITEM_IMAGE
    if requested == "video" or ext in {".mp4", ".mov", ".mkv", ".avi", ".webm"}:
        return UPLOAD_VIDEO, ITEM_VIDEO
    return UPLOAD_FILE, ITEM_FILE


def _parse_item_list(item_list: Any) -> Tuple[str, List[InboundAttachment], Optional[str]]:
    texts: List[str] = []
    attachments: List[InboundAttachment] = []
    reply_to: Optional[str] = None

    if not isinstance(item_list, list):
        return "", [], None

    for item in item_list:
        if not isinstance(item, dict):
            continue
        item_type = int(item.get("type") or 0)
        ref = item.get("ref_msg") if isinstance(item.get("ref_msg"), dict) else None
        if ref:
            reply_to = _reply_id_from_ref(ref) or reply_to

        if item_type == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            if ref:
                quoted = _quoted_text_from_ref(ref)
                if quoted:
                    text = f"[引用: {quoted}]\n{text}".strip()
            texts.append(text)
        elif item_type == ITEM_VOICE:
            voice_item = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
            transcript = str(voice_item.get("text") or "").strip()
            if transcript:
                texts.append(f"[语音转写]\n{transcript}")
            else:
                attachments.append(InboundAttachment(
                    modality="audio",
                    file_name="voice.silk",
                    description="微信语音",
                    metadata={"oc_item": item},
                ))
        elif item_type == ITEM_IMAGE:
            attachments.append(InboundAttachment(
                modality="image",
                file_name=_item_file_name(item, "image.jpg"),
                description="微信图片",
                metadata={"oc_item": item},
            ))
        elif item_type == ITEM_VIDEO:
            attachments.append(InboundAttachment(
                modality="video",
                file_name=_item_file_name(item, "video.mp4"),
                description="微信视频",
                metadata={"oc_item": item},
            ))
        elif item_type == ITEM_FILE:
            file_item = item.get("file_item") if isinstance(item.get("file_item"), dict) else {}
            file_name = str(file_item.get("file_name") or "file.bin")
            attachments.append(InboundAttachment(
                modality="file",
                file_name=Path(file_name).name or "file.bin",
                description=f"微信文件 {Path(file_name).name or 'file.bin'}",
                metadata={"oc_item": item},
            ))

    return "\n".join(part for part in texts if part).strip(), attachments, reply_to


def _item_file_name(item: Dict[str, Any], fallback: str) -> str:
    if int(item.get("type") or 0) == ITEM_FILE:
        raw = (item.get("file_item") or {}).get("file_name")
    elif int(item.get("type") or 0) == ITEM_VIDEO:
        raw = (item.get("video_item") or {}).get("file_name")
    else:
        raw = ""
    return Path(str(raw or fallback)).name or fallback


def _reply_id_from_ref(ref: Dict[str, Any]) -> Optional[str]:
    inner = ref.get("message_item") if isinstance(ref.get("message_item"), dict) else {}
    return str(inner.get("msg_id") or inner.get("message_id") or ref.get("message_id") or "") or None


def _quoted_text_from_ref(ref: Dict[str, Any]) -> str:
    parts: List[str] = []
    if ref.get("title"):
        parts.append(str(ref.get("title")))
    inner = ref.get("message_item") if isinstance(ref.get("message_item"), dict) else {}
    if inner:
        text, _, _ = _parse_item_list([inner])
        if text:
            parts.append(text)
    return " | ".join(parts)


__all__ = [
    "ITEM_FILE",
    "ITEM_IMAGE",
    "ITEM_TEXT",
    "ITEM_VIDEO",
    "ITEM_VOICE",
    "UPLOAD_FILE",
    "UPLOAD_IMAGE",
    "UPLOAD_VIDEO",
    "build_media_send_body",
    "build_text_send_body",
    "item_kind_for_path",
    "parse_wechat_message",
]
