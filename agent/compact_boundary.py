"""Compact boundary 边界标记与切片 —— 对齐 Claude Code。

设计要点：
- boundary 是一条 system 消息，content 是 LLM 生成的摘要文本，metadata.kind=
  "compact_boundary"。它表示"在这之前的所有原始消息都已被这条摘要替代"。
- 跨轮 history 仍然保留 boundary 之前的消息（用于审计/恢复），但每轮发给 LLM
  的请求只取 boundary 之后的部分（含 boundary 本身）。
- 这层不负责"是否触发 compact"；触发逻辑在 session.py 的 preflight 三级阈值。

与 Claude Code 的对应关系：
- `make_compact_boundary_message` ≈ CC 的 buildPostCompactMessages
- `find_last_compact_boundary_index` ≈ CC 的 findLastCompactBoundaryIndex
- `get_messages_after_compact_boundary` ≈ CC 的 getMessagesAfterCompactBoundary
"""

from __future__ import annotations

from typing import List

from core.message import Message


COMPACT_BOUNDARY_KIND = "compact_boundary"
COMPACT_BOUNDARY_PREFIX = "【上下文压缩】"


def make_compact_boundary_message(summary: str) -> Message:
    """构造 compact boundary 消息。

    使用 system 角色而不是 assistant：
    - assistant 角色会被部分模型解读为"模型自己说过这话"，可能扰乱风格；
    - system 角色更贴近"这是一段背景说明"的语义，CC 也是这么做的。

    summary 文本会带上【上下文压缩】前缀（若已有则不重复加），方便人工审计时
    一眼看出这是 compact 锚点。
    """
    content = (summary or "").strip()
    if not content.startswith(COMPACT_BOUNDARY_PREFIX):
        content = COMPACT_BOUNDARY_PREFIX + content
    msg = Message.create_system_message(content)
    msg.metadata = {"kind": COMPACT_BOUNDARY_KIND}
    return msg


def _is_compact_boundary(message: Message) -> bool:
    """判断一条消息是否是 compact boundary。"""
    metadata = message.metadata if isinstance(message.metadata, dict) else {}
    return str(metadata.get("kind") or "") == COMPACT_BOUNDARY_KIND


def find_last_compact_boundary_index(messages: List[Message]) -> int:
    """倒序查找最后一个 compact boundary 的下标。没有则返回 -1。

    多个 boundary 时取最后一个，等价于"以最近一次压缩为准"。这样多轮压缩后
    早期 boundary 不会再被注入到 prompt。
    """
    for idx in range(len(messages) - 1, -1, -1):
        if _is_compact_boundary(messages[idx]):
            return idx
    return -1


def get_messages_after_compact_boundary(messages: List[Message]) -> List[Message]:
    """切片：返回 boundary（含）之后的所有消息。无 boundary 时原样返回。

    注意切片包含 boundary 本身——boundary 的摘要文本就是模型理解早期上下文
    的唯一入口，丢掉就会失去早期记忆。
    """
    idx = find_last_compact_boundary_index(messages)
    if idx == -1:
        return list(messages)
    return list(messages[idx:])


__all__ = [
    "COMPACT_BOUNDARY_KIND",
    "COMPACT_BOUNDARY_PREFIX",
    "make_compact_boundary_message",
    "find_last_compact_boundary_index",
    "get_messages_after_compact_boundary",
]
