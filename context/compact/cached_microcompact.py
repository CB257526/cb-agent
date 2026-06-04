"""cached_microcompact —— 客户端模拟的 tool result 微压缩。

对应 claude-code/src/services/compact/cachedMicrocompact.ts(cache_edits 模式)。

Anthropic 原生 API 通过 cache_edits 块在 provider 端删除超过窗口的 tool result;
OpenAI 兼容 API 没有这个能力,这里在客户端模拟:

- 跟踪每条 role=tool 消息的 tool_call_id(注册顺序)
- 当活跃 tool result 数 > TRIGGER_THRESHOLD 时,删除最旧的,保留最近 KEEP_RECENT 条
- 删除手段: 把超出范围的 tool message 的 content 替换为简短占位文本

注意: 这只影响"下一轮发给 LLM 的 tool result 详情",不影响 work_context
跨轮工作记录的写入。后者已经在 work_context.py 里有自己的字符上限。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Sequence

from core.message import Message, MessageRole


logger = logging.getLogger(__name__)


TRIGGER_THRESHOLD = 10
KEEP_RECENT = 5
PLACEHOLDER_TEXT = "[tool result elided by cached_microcompact; see work record]"


@dataclass
class CachedMCState:
    """客户端模拟状态。一个 session 持一份。

    与 work_context 分工:
    - work_context: 跨轮工作记录,文本压缩与持久化
    - cached_microcompact: 单轮 messages 列表内,过老 tool result 的占位替换
    """

    elided_call_ids: set[str] = field(default_factory=set)


def is_cached_microcompact_supported(model: str) -> bool:
    """OpenAI 兼容 API 永远走客户端模拟。Anthropic provider 走原生 cache_edits。

    cb-agent 当前只走 OpenAI 兼容,直接 True。
    """
    del model
    return True


def get_active_tool_call_ids(messages: Sequence[Message]) -> List[str]:
    """按出现顺序提取所有 role=tool 消息的 tool_call_id(已 elide 的不计)。"""
    out: List[str] = []
    for m in messages:
        if m.role != MessageRole.TOOL:
            continue
        if not m.tool_call_id:
            continue
        out.append(m.tool_call_id)
    return out


def maybe_microcompact_tool_results(
    messages: List[Message],
    state: CachedMCState,
    *,
    trigger_threshold: int = TRIGGER_THRESHOLD,
    keep_recent: int = KEEP_RECENT,
) -> int:
    """检查并执行 tool result 占位替换。返回被 elide 的条数。

    原地修改 messages: tool message 的 content 被替换为占位文本,
    tool_call_id 加入 state.elided_call_ids。
    """
    active_ids = [
        cid for cid in get_active_tool_call_ids(messages)
        if cid not in state.elided_call_ids
    ]
    if len(active_ids) <= trigger_threshold:
        return 0
    to_elide = set(active_ids[: max(0, len(active_ids) - keep_recent)])
    if not to_elide:
        return 0
    elided = 0
    for m in messages:
        if m.role != MessageRole.TOOL:
            continue
        if not m.tool_call_id or m.tool_call_id not in to_elide:
            continue
        if m.tool_call_id in state.elided_call_ids:
            continue
        m.content = PLACEHOLDER_TEXT
        state.elided_call_ids.add(m.tool_call_id)
        elided += 1
    if elided:
        logger.info(
            "cached_microcompact elided %d tool result(s) (threshold=%d, keep=%d)",
            elided,
            trigger_threshold,
            keep_recent,
        )
    return elided


__all__ = [
    "CachedMCState",
    "KEEP_RECENT",
    "PLACEHOLDER_TEXT",
    "TRIGGER_THRESHOLD",
    "get_active_tool_call_ids",
    "is_cached_microcompact_supported",
    "maybe_microcompact_tool_results",
]
