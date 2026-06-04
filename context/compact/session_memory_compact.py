"""SessionMemoryCompact —— 利用本地 SessionState 直接生成摘要(零 API 调用)。

对应 claude-code/src/services/compact/sessionMemoryCompact.ts。

思路: cb-agent 的 LocalSessionStore.state_text() 已经是"滚动工作态"的可读
文本(active_task / files_seen / decisions / pending),非常适合直接当摘要
注入下一轮上下文。这条路径成本 0,失败时再降级到 LLM 摘要。
"""

from __future__ import annotations

from typing import Any, Optional


def try_session_memory_summary(session_state: Any) -> Optional[str]:
    """从 SessionState 提取摘要文本。

    session_state 可以是任何提供 state_text() 方法的对象(典型: LocalSessionStore)。
    返回 None 表示"无可用素材",由调用方降级到 LLM summarizer。
    """
    if session_state is None:
        return None
    state_text_fn = getattr(session_state, "state_text", None)
    if not callable(state_text_fn):
        return None
    try:
        text = state_text_fn()
    except Exception:
        return None
    if not text or not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None
    return (
        "前序工作态(由本地 SessionState 直接生成,未调用 LLM):\n"
        + text
    )


__all__ = ["try_session_memory_summary"]
