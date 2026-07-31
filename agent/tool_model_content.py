"""把工具终态携带的结构化模型内容转换成正式 history 消息。"""

from __future__ import annotations

import copy
from typing import Any, Sequence

from core.media import ImageRef
from core.message import Message, MessageRole


TOOL_MODEL_CONTENT_KIND = "tool_image_bridge"


def _part_label(part: dict[str, Any]) -> str:
    """为 bridge 生成不含本地路径和图片正文的短标签。"""

    if part.get("type") == "image_ref":
        ref = part.get("image_ref") if isinstance(part.get("image_ref"), dict) else {}
        file_name = str(ref.get("file_name") or "image")
        return f"图片加载成功：{file_name}"
    return "工具已返回结构化模型内容"


def tool_model_content_call_ids(messages: Sequence[Message]) -> set[str]:
    """读取一组 bridge 已覆盖的工具调用 ID。"""

    result: set[str] = set()
    for message in messages:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        if metadata.get("kind") != TOOL_MODEL_CONTENT_KIND:
            continue
        result.update(
            str(call_id)
            for call_id in metadata.get("tool_call_ids", [])
            if str(call_id)
        )
    return result


def build_tool_model_content_bridge(
    tool_messages: Sequence[Message],
    *,
    excluded_call_ids: Sequence[str] = (),
) -> Message | None:
    """按工具声明顺序合并尚未进入 history 的结构化模型内容。"""

    excluded = {str(call_id) for call_id in excluded_call_ids if str(call_id)}
    content: list[dict[str, Any]] = []
    call_ids: list[str] = []
    for message in tool_messages:
        call_id = str(message.tool_call_id or "")
        if call_id and call_id in excluded:
            continue
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        parts = metadata.get("model_content")
        if not isinstance(parts, list) or not parts:
            continue
        if call_id:
            call_ids.append(call_id)
        for part in parts:
            if not isinstance(part, dict) or part.get("type") != "image_ref":
                raise ValueError("工具 model_content 含不受支持的内容块")
            # journal 恢复也走这里；损坏引用必须阻止启动，不能静默丢图继续。
            ImageRef.from_dict(part.get("image_ref") or {})
            content.append({"type": "text", "text": _part_label(part)})
            content.append(copy.deepcopy(part))
    if not content:
        return None
    return Message(
        role=MessageRole.USER,
        content=content,
        metadata={
            "kind": TOOL_MODEL_CONTENT_KIND,
            "tool_call_ids": call_ids,
        },
    )


__all__ = [
    "TOOL_MODEL_CONTENT_KIND",
    "build_tool_model_content_bridge",
    "tool_model_content_call_ids",
]
