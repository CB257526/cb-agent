"""Compact boundary 的唯一实现。"""

from __future__ import annotations

from typing import Optional, Sequence

from core.message import Message, MessageRole


COMPACT_BOUNDARY_KIND = "compact_boundary"
COMPACT_BOUNDARY_PREFIX = "【上下文压缩】"


def make_compact_boundary_message(
    summary: str,
    *,
    tokens_before: int = 0,
    tokens_after: int = 0,
    reason: str = "",
) -> Message:
    """生成模型可见的 user-role compact 摘要消息。

    Chat Completions 兼容服务对第二条 system 消息的处理不一致，因此摘要使用 user
    角色，并通过 metadata 标记本地语义。摘要是 replacement history 的第一条消息。
    """
    content = (summary or "").strip()
    if not content.startswith(COMPACT_BOUNDARY_PREFIX):
        content = COMPACT_BOUNDARY_PREFIX + content
    return Message(
        role=MessageRole.USER,
        content=content,
        metadata={
            "kind": COMPACT_BOUNDARY_KIND,
            "tokens_before": int(tokens_before),
            "tokens_after": int(tokens_after),
            "reason": reason or "",
        },
    )


def is_compact_boundary(message: Message) -> bool:
    """判断消息是否为 compact boundary，兼容旧 system-role boundary。"""
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("kind") or "") == COMPACT_BOUNDARY_KIND


def find_last_compact_boundary_index(messages: Sequence[Message]) -> int:
    """返回最后一个 compact boundary 下标；不存在时返回 -1。"""
    for index in range(len(messages) - 1, -1, -1):
        if is_compact_boundary(messages[index]):
            return index
    return -1


def find_last_compact_boundary(messages: Sequence[Message]) -> Optional[int]:
    """兼容旧 Optional 下标接口。"""
    index = find_last_compact_boundary_index(messages)
    return None if index < 0 else index


def get_messages_after_compact_boundary(messages: Sequence[Message]) -> list[Message]:
    """返回最后一个 boundary 及其后的 active history。"""
    index = find_last_compact_boundary_index(messages)
    return list(messages if index < 0 else messages[index:])


def messages_after_last_boundary(messages: Sequence[Message]) -> list[Message]:
    """兼容旧命名。"""
    return get_messages_after_compact_boundary(messages)


__all__ = [
    "COMPACT_BOUNDARY_KIND",
    "COMPACT_BOUNDARY_PREFIX",
    "find_last_compact_boundary",
    "find_last_compact_boundary_index",
    "get_messages_after_compact_boundary",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "messages_after_last_boundary",
]
