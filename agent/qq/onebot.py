"""OneBot V11 消息解析与发送消息段转换。"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

from agent.platforms.messages import ConversationKey, InboundAttachment, InboundMessage, OutboundSegment
from agent.qq.config import QQConfig
from agent.qq.file_delivery import is_external_file_reference, looks_like_posix_absolute_path

_CQ_CODE_RE = re.compile(r"\[CQ:([A-Za-z0-9_]+)((?:,[^\]]*)?)\]")


def parse_onebot_message_event(
    event: Dict[str, Any],
    config: QQConfig,
    *,
    require_wakeup: bool = True,
) -> Optional[InboundMessage]:
    """把 OneBot V11 message 事件转换为平台无关入站消息。

    返回 None 表示该事件不需要触发 Agent，例如群聊未唤醒、白名单不匹配、非消息事件。
    """

    if event.get("post_type") != "message":
        return None
    message_type = str(event.get("message_type") or "")
    if message_type not in {"private", "group"}:
        return None

    user_id = str(event.get("user_id") or "")
    group_id = str(event.get("group_id") or "")
    if config.allowed_users and user_id not in config.allowed_users:
        return None
    if message_type == "group" and config.allowed_groups and group_id not in config.allowed_groups:
        return None

    text, attachments, mentioned, reply_to = _parse_message_segments(event.get("message"), str(event.get("self_id") or ""))
    text = _strip_cq_codes(text).strip()

    if message_type == "group" and require_wakeup:
        text = _apply_group_wakeup(text=text, mentioned=mentioned, config=config)
        if text is None:
            return None

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    conversation = ConversationKey(
        platform="qq",
        kind="group" if message_type == "group" else "private",
        id=group_id if message_type == "group" else user_id,
    )
    return InboundMessage(
        conversation=conversation,
        sender_id=user_id,
        sender_name=str(sender.get("card") or sender.get("nickname") or user_id),
        text=text,
        raw=event,
        attachments=attachments,
        message_id=str(event.get("message_id") or "") or None,
        reply_to_message_id=reply_to,
    )


def parse_onebot_event(
    event: Dict[str, Any],
    config: QQConfig,
    *,
    require_wakeup: bool = True,
) -> Optional[InboundMessage]:
    """统一解析 OneBot V11 入站事件。

    只有 ``message`` 事件表示用户真的发了一句话给机器人，才允许触发 Agent。
    ``notice`` / ``request`` 是平台状态或管理事件，例如群文件上传、戳一戳、输入
    状态、好友申请等；它们没有普通对话上下文，默认静默，避免机器人自己发文件后被
    群文件上传回声再次触发。
    """

    post_type = str(event.get("post_type") or "")
    if post_type == "message":
        return parse_onebot_message_event(event, config, require_wakeup=require_wakeup)
    return None


def outbound_segment_to_onebot(segment: OutboundSegment) -> List[Dict[str, Any]]:
    """把平台无关出站段转换为 OneBot V11 消息段。

    普通文件在 NapCat 中也可以走消息段 file；如果实际平台不支持，适配器会根据
    action 响应再降级文本提示。
    """

    kind = segment.kind
    if kind in {"text", "status", "todo", "question"}:
        return [{"type": "text", "data": {"text": segment.text}}] if segment.text else []
    if kind in {"image", "sticker"}:
        return [{"type": "image", "data": {"file": _local_file_uri(segment.path)}}]
    if kind == "audio":
        return [{"type": "record", "data": {"file": _local_file_uri(segment.path)}}]
    if kind == "video":
        return [{"type": "video", "data": {"file": _local_file_uri(segment.path)}}]
    if kind == "file":
        return [{"type": "file", "data": {"file": _local_file_uri(segment.path), "name": segment.file_name}}]
    return [{"type": "text", "data": {"text": segment.text or f"[不支持的消息段: {kind}]"}}]


def _parse_message_segments(message: Any, self_id: str) -> Tuple[str, List[InboundAttachment], bool, Optional[str]]:
    """解析 OneBot 消息段。

    返回值最后一个 ``reply_to`` 是引用消息 ID。这里不直接调用 ``get_msg``，因为解析器
    保持纯函数，真正需要访问 NapCat action 的补全工作交给 adapter 做。
    """

    if isinstance(message, str):
        return _parse_cq_string_message(message, self_id)
    if not isinstance(message, list):
        return str(message or ""), [], False, None

    texts: List[str] = []
    attachments: List[InboundAttachment] = []
    mentioned = False
    reply_to: Optional[str] = None
    for seg in message:
        if not isinstance(seg, dict):
            continue
        seg_type = str(seg.get("type") or "")
        data = seg.get("data") if isinstance(seg.get("data"), dict) else {}
        if seg_type == "text":
            texts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "")
            if self_id and qq == self_id:
                mentioned = True
            texts.append(f"@{qq} ")
        elif seg_type == "image":
            url = str(data.get("url") or "")
            file_name = str(data.get("file") or Path(urlparse(url).path).name or "image")
            attachments.append(InboundAttachment(
                modality="image",
                url=url or None,
                file_name=file_name,
                description=f"QQ 图片 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))
        elif seg_type in {"record", "audio"}:
            file_name = str(data.get("file") or "audio")
            attachments.append(InboundAttachment(
                modality="audio",
                url=str(data.get("url") or "") or None,
                file_name=file_name,
                description=f"QQ 音频 {file_name}",
                metadata=dict(data),
            ))
        elif seg_type == "file":
            file_name = str(data.get("name") or data.get("file_name") or data.get("file") or "file")
            file_id = str(data.get("file_id") or data.get("id") or "") or None
            url = str(data.get("url") or "") or None
            attachments.append(InboundAttachment(
                modality="file",
                url=url,
                file_id=file_id,
                file_name=file_name,
                description=f"QQ 文件 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))
        elif seg_type == "reply":
            reply_to = str(data.get("id") or data.get("message_id") or "") or reply_to
            if reply_to:
                texts.append(f"[引用消息 {reply_to}] ")
    return "".join(texts).strip(), attachments, mentioned, reply_to


def _parse_cq_string_message(message: str, self_id: str) -> Tuple[str, List[InboundAttachment], bool, Optional[str]]:
    """解析 OneBot 字符串消息格式。

    NapCat/OneBot 可以把消息发成数组段，也可以发成 ``[CQ:image,...]`` 字符串。
    后者如果只做正则清理，会丢掉图片、语音等附件；这里把常见 CQ 码恢复成和数组段
    相同的 InboundAttachment，保证不同 message_format 下后端行为一致。
    """

    texts: List[str] = []
    attachments: List[InboundAttachment] = []
    mentioned = False
    reply_to: Optional[str] = None
    last = 0
    for match in _CQ_CODE_RE.finditer(message):
        if match.start() > last:
            texts.append(_unescape_cq_value(message[last:match.start()]))
        seg_type = match.group(1).lower()
        data = _parse_cq_params(match.group(2))
        if seg_type == "at":
            qq = str(data.get("qq") or "")
            if self_id and qq == self_id:
                mentioned = True
            texts.append(f"@{qq} ")
        elif seg_type == "image":
            url = str(data.get("url") or "")
            file_name = str(data.get("file") or Path(urlparse(url).path).name or "image")
            attachments.append(InboundAttachment(
                modality="image",
                url=url or None,
                file_name=file_name,
                description=f"QQ 图片 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))
        elif seg_type in {"record", "audio"}:
            url = str(data.get("url") or "")
            file_name = str(data.get("file") or Path(urlparse(url).path).name or "audio")
            attachments.append(InboundAttachment(
                modality="audio",
                url=url or None,
                file_name=file_name,
                description=f"QQ 音频 {file_name}",
                metadata=dict(data),
            ))
        elif seg_type == "file":
            url = str(data.get("url") or "")
            file_name = str(data.get("name") or data.get("file_name") or data.get("file") or Path(urlparse(url).path).name or "file")
            file_id = str(data.get("file_id") or data.get("id") or "") or None
            attachments.append(InboundAttachment(
                modality="file",
                url=url or None,
                file_id=file_id,
                file_name=file_name,
                description=f"QQ 文件 {file_name}" + (f" URL={url}" if url else ""),
                metadata=dict(data),
            ))
        elif seg_type == "reply":
            reply_to = str(data.get("id") or data.get("message_id") or "") or reply_to
            if reply_to:
                texts.append(f"[引用消息 {reply_to}] ")
        last = match.end()
    if last < len(message):
        texts.append(_unescape_cq_value(message[last:]))
    return "".join(texts).strip(), attachments, mentioned, reply_to


def _parse_cq_params(raw: str) -> Dict[str, str]:
    """解析 CQ 码参数。

    CQ 参数里的逗号、方括号、& 会被转义成 HTML 实体；先按未转义逗号切分，再统一
    html.unescape，可以覆盖 OneBot V11 的常见转义写法。
    """

    result: Dict[str, str] = {}
    value = raw[1:] if raw.startswith(",") else raw
    if not value:
        return result
    for item in value.split(","):
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        key = key.strip()
        if key:
            result[key] = _unescape_cq_value(val)
    return result


def _unescape_cq_value(value: str) -> str:
    return html.unescape(value)


def _apply_group_wakeup(*, text: str, mentioned: bool, config: QQConfig) -> Optional[str]:
    mode = config.group_mode
    prefix = config.wake_prefix
    clean = text.strip()
    if mode == "all":
        return clean
    if mode == "mention":
        if mentioned:
            return _strip_at_tokens(clean)
        if prefix and clean.startswith(prefix):
            return clean[len(prefix):].strip()
        return None
    if mode == "prefix":
        if prefix and clean.startswith(prefix):
            return clean[len(prefix):].strip()
        return None
    return None


def _strip_cq_codes(text: str) -> str:
    return re.sub(r"\[CQ:[^\]]+\]", "", text)


def _contains_cq_at_self(text: str, self_id: str) -> bool:
    if not self_id:
        return False
    return f"qq={self_id}" in text and "[CQ:at" in text


def _strip_at_tokens(text: str) -> str:
    return re.sub(r"@\d+\s*", "", text).strip()


def _local_file_uri(path: str) -> str:
    if is_external_file_reference(path) or looks_like_posix_absolute_path(path):
        return str(path)
    p = Path(path).expanduser().resolve()
    try:
        return p.as_uri()
    except ValueError:
        return "file://" + quote(str(p).replace("\\", "/"))


__all__ = ["parse_onebot_event", "parse_onebot_message_event", "outbound_segment_to_onebot"]
