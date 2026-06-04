"""auto_compact —— 自动压缩触发与执行。

对应 claude-code/src/services/compact/autoCompact.ts。

触发流程(对齐 Claude Code 设计):
1. 估算当前 messages 的 token 总量
2. 与 context_window * threshold_pct 比较;未达不触发
3. 找最后一次 compact_boundary,只压缩它之后的消息(不重复压缩)
4. 优先尝试 try_session_memory_summary (零 API 调用)
5. fallback: summarizer.summarize(...) (LLM 调用)
6. 用 make_compact_boundary_message 替换待压缩段,保留最近 N 条原始消息

阈值默认: 0.85(对齐 ConstantLLM.CONTEXT_USAGE_RATIO=0.8 略高 5%,避免和
ContextBuilder 的预算判断重复触发,实测 0.85 在国内 OpenAI 兼容 API 上比
0.80 更稳)。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from core.message import Message

from ..budget.tokens import count_tokens
from ..budget.window import get_context_window_for_model
from .boundary import (
    find_last_compact_boundary,
    make_compact_boundary_message,
)
from .session_memory_compact import try_session_memory_summary
from .summarizer import RuleBasedSummarizer, Summarizer


logger = logging.getLogger(__name__)


DEFAULT_THRESHOLD_PCT = 0.85
DEFAULT_KEEP_RECENT = 6


@dataclass
class AutoCompactResult:
    """压缩结果。triggered=False 时其它字段仅供调试。"""

    triggered: bool
    reason: str
    tokens_before: int
    tokens_after: int
    summary: Optional[str]
    boundary_index: Optional[int]


def _estimate_messages_tokens(messages: Sequence[Message]) -> int:
    """粗略估算 messages 总 token 数。

    与 session.py 中现有 _context_message_line 逻辑保持一致: role + kind + content。
    不包括 tool_calls / tool_call_id 协议字段(它们仅在单轮内有效)。
    """
    total = 0
    for m in messages:
        role = m.role.value if hasattr(m.role, "value") else str(m.role)
        meta = m.metadata if isinstance(m.metadata, dict) else {}
        kind = str(meta.get("kind") or "")
        content = m.content if isinstance(m.content, str) else _flatten(m.content)
        line = f"{role}/{kind}: {content}" if kind else f"{role}: {content}"
        total += count_tokens(line)
    return total


def _flatten(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                out.append(str(item.get("text") or ""))
            elif isinstance(item, dict):
                out.append(f"[{item.get('type', 'item')}]")
            else:
                out.append(str(item))
        return " ".join(out)
    return str(content)


async def maybe_auto_compact(
    messages: List[Message],
    *,
    model: str,
    summarizer: Optional[Summarizer] = None,
    session_state: Any = None,
    threshold_pct: float = DEFAULT_THRESHOLD_PCT,
    keep_recent_messages: int = DEFAULT_KEEP_RECENT,
    force: bool = False,
    focus: Optional[str] = None,
) -> AutoCompactResult:
    """检查并执行自动压缩。原地修改 messages 列表。

    force=True 跳过阈值检查(用户主动 /compact)。
    """
    window = get_context_window_for_model(model)
    threshold = max(1, int(window * max(0.1, min(threshold_pct, 0.99))))
    tokens_before = _estimate_messages_tokens(messages)

    if not force and tokens_before < threshold:
        return AutoCompactResult(
            triggered=False,
            reason=f"under_threshold(tokens={tokens_before},limit={threshold})",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            summary=None,
            boundary_index=None,
        )

    # 找最后一次 boundary,只压缩它之后的消息
    last_boundary = find_last_compact_boundary(messages)
    base_idx = 0 if last_boundary is None else last_boundary + 1

    # 保留最近 keep_recent_messages 条不动
    upper_idx = max(base_idx, len(messages) - keep_recent_messages)
    to_summarize = messages[base_idx:upper_idx]

    if not to_summarize:
        return AutoCompactResult(
            triggered=False,
            reason="nothing_to_summarize",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            summary=None,
            boundary_index=last_boundary,
        )

    # 路径 1: SessionState 摘要(零 API 调用)
    summary = try_session_memory_summary(session_state)
    used_route = "session_memory"

    # 路径 2: LLM summarizer
    if not summary and summarizer is not None:
        try:
            summary = await summarizer.summarize(to_summarize, focus=focus)
            used_route = "llm_summarizer"
        except Exception as e:
            logger.warning("auto_compact: summarizer failed: %s", e)
            summary = None

    # 路径 3: 规则兜底
    if not summary:
        rule = RuleBasedSummarizer()
        summary = await rule.summarize(to_summarize, focus=focus)
        used_route = "rule_based"

    if not summary:
        return AutoCompactResult(
            triggered=False,
            reason="no_summary_available",
            tokens_before=tokens_before,
            tokens_after=tokens_before,
            summary=None,
            boundary_index=last_boundary,
        )

    boundary = make_compact_boundary_message(
        summary=summary,
        tokens_before=tokens_before,
        reason=f"auto_compact:{used_route}",
    )
    new_messages = messages[:base_idx] + [boundary] + messages[upper_idx:]
    messages.clear()
    messages.extend(new_messages)

    tokens_after = _estimate_messages_tokens(messages)
    boundary_idx = base_idx
    boundary.metadata["tokens_after"] = tokens_after  # type: ignore[index]

    logger.info(
        "auto_compact triggered: route=%s tokens %d -> %d (kept_recent=%d)",
        used_route,
        tokens_before,
        tokens_after,
        keep_recent_messages,
    )
    return AutoCompactResult(
        triggered=True,
        reason=f"auto_compact:{used_route}",
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        summary=summary,
        boundary_index=boundary_idx,
    )


__all__ = [
    "AutoCompactResult",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_THRESHOLD_PCT",
    "maybe_auto_compact",
]
