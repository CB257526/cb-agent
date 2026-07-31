"""为 compact 摘要请求生成不含图片正文的派生视图。"""

from __future__ import annotations

from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

from core.media import ImageRef
from core.message import Message


def _image_ref_marker(value: Any, *, call_ids: Sequence[str]) -> str:
    """把 ImageRef 转成摘要模型可理解、但不可反向污染 history 的清单项。"""

    try:
        ref = ImageRef.from_dict(value or {})
    except (TypeError, ValueError):
        return "[image: invalid ImageRef omitted]"
    size = (
        f"{ref.width}x{ref.height}"
        if ref.width is not None and ref.height is not None
        else "unknown"
    )
    call_text = f", call_ids={','.join(call_ids)}" if call_ids else ""
    return (
        "[image: "
        f"file={ref.file_name or 'image'}, mime={ref.mime_type}, "
        f"sha256={ref.sha256}, bytes={ref.byte_size}, size={size}{call_text}]"
    )


def _legacy_image_marker(value: Any) -> str:
    """描述旧 image_url，绝不让摘要 provider 读取 data URI 或远程 URL。"""

    image_url = value.get("image_url") if isinstance(value, dict) else None
    url = image_url.get("url") if isinstance(image_url, dict) else None
    if not isinstance(url, str) or not url:
        return "[legacy image: missing URL omitted]"
    if url.startswith("data:"):
        header = url.split(",", 1)[0]
        mime = header[5:].split(";", 1)[0] if header.startswith("data:") else "unknown"
        return f"[legacy image data omitted: mime={mime}, chars={len(url)}]"
    parsed = urlsplit(url)
    # query 和 fragment 可能包含签名或凭证；摘要只需要稳定的来源轮廓。
    safe_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    return f"[legacy remote image omitted: url={safe_url}]"


def _content_view(value: Any, *, call_ids: Sequence[str]) -> Any:
    """递归替换图片内容块，并保留其它 provider 协议字段。"""

    if isinstance(value, list):
        return [_content_view(item, call_ids=call_ids) for item in value]
    if not isinstance(value, dict):
        return value
    part_type = value.get("type")
    if part_type == "image_ref":
        return {
            "type": "text",
            "text": _image_ref_marker(value.get("image_ref"), call_ids=call_ids),
        }
    if part_type == "image_url":
        return {"type": "text", "text": _legacy_image_marker(value)}
    return {
        key: _content_view(item, call_ids=call_ids)
        for key, item in value.items()
    }


def build_compaction_view(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """生成摘要专用消息；原消息及 retained tail 均不会被修改。"""

    result: list[dict[str, Any]] = []
    for message in messages:
        payload = message.to_dict()
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        call_ids = tuple(
            str(item)
            for item in metadata.get("tool_call_ids", [])
            if str(item)
        )
        payload["content"] = _content_view(payload.get("content"), call_ids=call_ids)
        result.append(payload)
    return result


__all__ = ["build_compaction_view"]
