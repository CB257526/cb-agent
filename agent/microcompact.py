"""Microcompact —— 用 LRU 替换最旧的 tool_result content。

背景：
- Claude Code 的 cached_microcompact 依赖 Anthropic API 的 cache_edits 字段，
  本地消息原封不动，让 server 端忽略旧 tool_result。
- cb-agent 走 OpenAI 兼容协议（DeepSeek 等），没有这个能力。这里用同等语义的
  本地策略替代：在传给 LLM 前，原地替换最旧的若干条 tool 消息的 content 为
  占位符，但保留 tool_call_id 和 name，确保 OpenAI 协议的配对仍然合法。

触发条件：
- messages 里 role=tool 的消息数 >= MICROCOMPACT_THRESHOLD (10)
- 保留最近 MICROCOMPACT_KEEP_RECENT (5) 条原文，更旧的全替换为占位

注意：
- 这一层只动 tool 消息的 content 字段，不动 assistant.tool_calls，不删任何消息。
- 替换是幂等的：被替换过的消息 content 已是占位，下一次扫描会跳过。
- 这一层早于 autocompact 触发，目的是在更省事的层级释放 token；如果不够还有
  autocompact 兜底。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


# tool_result 数达到阈值时开始 LRU 替换
MICROCOMPACT_THRESHOLD = 10
# 保留最近 N 条 tool_result 原文，更旧的替换为占位
MICROCOMPACT_KEEP_RECENT = 5

# 占位符内容。用 JSON 形式与正常 tool result 保持结构一致，防止下游 JSON
# 解析逻辑遇到纯文本占位时报错。
CLEARED_PLACEHOLDER = json.dumps(
    {
        "cleared": True,
        "hint": "[旧工具结果内容已清理 —— microcompact LRU 释放上下文]",
    },
    ensure_ascii=False,
)


def _is_already_cleared(content: Any) -> bool:
    """判断一条 tool 消息是否已经被 microcompact 替换过，避免重复处理。"""
    if not isinstance(content, str):
        return False
    if not content.startswith('{"cleared":'):
        return False
    try:
        data = json.loads(content)
        return isinstance(data, dict) and data.get("cleared") is True
    except (json.JSONDecodeError, TypeError):
        return False


def apply_microcompact(messages: List[Dict[str, Any]]) -> int:
    """原地 LRU 替换最旧的 tool_result content。返回被清理的条数。

    扫描策略：
    1. 收集所有 role=tool 消息的下标（按 messages 中出现顺序）；
    2. 跳过已被清理过的；
    3. 若剩余原文条数 > KEEP_RECENT，把"最旧 (剩余 - KEEP_RECENT)"条替换为占位。

    被替换的消息保留 tool_call_id / name / role，只有 content 被换。这是 OpenAI
    协议合法性的最低要求：assistant.tool_calls 与对应 role=tool 的配对依赖
    tool_call_id 而非 content。
    """
    if not messages:
        return 0

    tool_indices: List[int] = []
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue
        if _is_already_cleared(msg.get("content")):
            continue
        tool_indices.append(idx)

    if len(tool_indices) < MICROCOMPACT_THRESHOLD:
        return 0

    # 需要清理的数量 = 总条数 - KEEP_RECENT
    to_clear = len(tool_indices) - MICROCOMPACT_KEEP_RECENT
    if to_clear <= 0:
        return 0

    cleared_count = 0
    for idx in tool_indices[:to_clear]:
        messages[idx]["content"] = CLEARED_PLACEHOLDER
        cleared_count += 1

    if cleared_count > 0:
        logger.info(
            "microcompact: cleared %s old tool_result contents (kept %s recent, total %s)",
            cleared_count,
            MICROCOMPACT_KEEP_RECENT,
            len(tool_indices),
        )
    return cleared_count


__all__ = [
    "MICROCOMPACT_THRESHOLD",
    "MICROCOMPACT_KEEP_RECENT",
    "CLEARED_PLACEHOLDER",
    "apply_microcompact",
]
