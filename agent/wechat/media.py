"""微信 OC 入站媒体保存与出站媒体辅助。"""

from __future__ import annotations

import mimetypes
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from agent.platforms.messages import InboundAttachment
from agent.wechat.client import WeChatOCClient
from agent.wechat.config import WeChatConfig
from agent.wechat.oc_types import ITEM_FILE, ITEM_IMAGE, ITEM_VIDEO, ITEM_VOICE


def materialize_inbound_attachment(
    attachment: InboundAttachment,
    *,
    client: WeChatOCClient,
    config: WeChatConfig,
) -> None:
    """把微信 OC 加密媒体下载到本地。

    多模态输入层目前只接收 image/audio 本地路径；文件和视频先保留为文本提示，
    真实本地路径放在 metadata 里，避免误把 file/video 传给 OCR/ASR 路径导致本轮失败。
    """

    item = attachment.metadata.get("oc_item") if isinstance(attachment.metadata, dict) else None
    if not isinstance(item, dict):
        return

    item_type = int(item.get("type") or 0)
    media = _media_payload(item_type, item)
    if not media:
        return
    encrypted_query_param = str(media.get("encrypt_query_param") or "").strip()
    full_url = str(media.get("full_url") or "").strip()
    aes_key = _aes_key_for_item(item_type, item, media)
    if not encrypted_query_param and not full_url:
        return

    data = client.download_media(
        encrypted_query_param=encrypted_query_param,
        aes_key_value=aes_key,
        full_url=full_url,
    )
    file_name = _safe_name(attachment.file_name or _fallback_name(item_type))
    target = _target_path(config.attachment_dir, file_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)

    attachment.metadata["saved_path"] = str(target)
    attachment.metadata["size"] = len(data)
    if attachment.modality == "image":
        attachment.path = str(target)
    elif attachment.modality == "audio" and target.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}:
        attachment.path = str(target)
    elif attachment.modality == "audio":
        # 微信语音常见为 SILK。当前多模态 ASR 层不支持 .silk，直接塞给附件管线
        # 会导致整轮失败；先作为本地临时文件提示保留，后续可再接 SILK 转 WAV。
        attachment.description = f"微信语音已保存到本地临时路径：{target}；当前 ASR 附件管线暂不支持该编码。"
    else:
        attachment.description = f"{attachment.description or '微信附件'}，已保存到本地临时路径：{target}"


def mime_for_path(path: str | Path) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def _media_payload(item_type: int, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if item_type == ITEM_IMAGE:
        image = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
        return image.get("media") if isinstance(image.get("media"), dict) else None
    if item_type == ITEM_VIDEO:
        video = item.get("video_item") if isinstance(item.get("video_item"), dict) else {}
        return video.get("media") if isinstance(video.get("media"), dict) else None
    if item_type == ITEM_FILE:
        file_item = item.get("file_item") if isinstance(item.get("file_item"), dict) else {}
        return file_item.get("media") if isinstance(file_item.get("media"), dict) else None
    if item_type == ITEM_VOICE:
        voice = item.get("voice_item") if isinstance(item.get("voice_item"), dict) else {}
        return voice.get("media") if isinstance(voice.get("media"), dict) else None
    return None


def _aes_key_for_item(item_type: int, item: Dict[str, Any], media: Dict[str, Any]) -> str:
    if item_type == ITEM_IMAGE:
        image = item.get("image_item") if isinstance(item.get("image_item"), dict) else {}
        raw_hex = str(image.get("aeskey") or "").strip()
        if raw_hex:
            return raw_hex
    return str(media.get("aes_key") or "").strip()


def _fallback_name(item_type: int) -> str:
    if item_type == ITEM_IMAGE:
        return "image.jpg"
    if item_type == ITEM_VIDEO:
        return "video.mp4"
    if item_type == ITEM_VOICE:
        return "voice.silk"
    return "file.bin"


def _safe_name(name: str) -> str:
    path_name = Path(name or "file.bin").name
    return path_name or "file.bin"


def _target_path(root: Path, file_name: str) -> Path:
    root = root.expanduser()
    if not root.is_absolute():
        root = Path.cwd() / root
    stem = Path(file_name).stem or "wechat"
    suffix = Path(file_name).suffix or ""
    return (root / f"{stem}-{uuid.uuid4().hex[:10]}{suffix}").resolve()


__all__ = ["materialize_inbound_attachment", "mime_for_path"]
