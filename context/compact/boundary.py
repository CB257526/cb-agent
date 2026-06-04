"""CompactBoundary —— 消息列表中的"压缩边界"标记。

对应 claude-code 中 CompactBoundary marker 的概念。

设计要点:
- 不引入新 role,复用 Message.metadata["kind"]="compact_boundary"。
  LLM 看到的是普通 role=user 含 summary 的消息,不知元角色。
- 物理位置 = 压缩点。位置之前的消息已被 summary 替代;之后的是保留尾段。
- find_last_compact_boundary 取最后一次压缩点;auto_compact 每次都基于
  这个点之后的消息计算 token,不重复压缩已压缩段。
- 与 work_context.make_compact_record_message 的关系:后者本身就是"压缩
  记录消息",make_compact_boundary_message 是它的标准化版本,前者保留为
  薄 wrapper(整合点见 work_context.py 修改)。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from core.message import Message, MessageRole


COMPACT_BOUNDARY_KIND = "compact_boundary"


def make_compact_boundary_message(
    *,
    summary: str,
    tokens_before: int = 0,
    tokens_after: int = 0,
    reason: str = "",
) -> Message:
    """生成一条 role=user, kind=compact_boundary 的标记消息。

    summary 会被 LLM 当作普通 user 消息读到。tokens_before/after/reason
    只写在 metadata,LLM 不可见,仅供 TUI/日志使用。

    role 选 user 而非 assistant: 让模型把摘要当作"用户告诉我已经发生过的事
    实",不会误以为是自己说过的话(避免 self-conditioning 偏差)。
    """
    return Message(
        role=MessageRole.USER,
        content=summary,
        metadata={
            "kind": COMPACT_BOUNDARY_KIND,
            "tokens_before": int(tokens_before),
            "tokens_after": int(tokens_after),
            "reason": reason or "",
        },
    )


def is_compact_boundary(message: Message) -> bool:
    meta = message.metadata if isinstance(message.metadata, dict) else None
    if not meta:
        return False
    return str(meta.get("kind") or "") == COMPACT_BOUNDARY_KIND


def find_last_compact_boundary(messages: Sequence[Message]) -> Optional[int]:
    """返回最后一条 compact_boundary 消息的下标;无则 None。"""
    for i in range(len(messages) - 1, -1, -1):
        if is_compact_boundary(messages[i]):
            return i
    return None


def messages_after_last_boundary(messages: Sequence[Message]) -> List[Message]:
    """返回最后一次压缩点之后的消息(含边界自身)。

    无边界时返回原列表的浅拷贝。auto_compact 用这个判断"自上次压缩以来
    新累积了多少 token"。
    """
    idx = find_last_compact_boundary(messages)
    if idx is None:
        return list(messages)
    return list(messages[idx:])


__all__ = [
    "COMPACT_BOUNDARY_KIND",
    "find_last_compact_boundary",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "messages_after_last_boundary",
]
