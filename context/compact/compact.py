"""compact —— 用户主动触发的压缩入口(/compact 命令)。

与 auto_compact 的差别:
- force=True (跳过阈值)
- 触发后清 SystemPromptSectionCache + MemoryLoader.cache(让下一轮重读)
- 接受 focus 提示,让摘要偏向某主题
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional

from core.message import Message

from ..sections.cache import clear_system_prompt_sections
from .auto_compact import (
    AutoCompactResult,
    DEFAULT_KEEP_RECENT,
    maybe_auto_compact,
)
from .summarizer import Summarizer


logger = logging.getLogger(__name__)


async def compact_now(
    messages: List[Message],
    *,
    model: str,
    summarizer: Optional[Summarizer] = None,
    session_state: Any = None,
    memory_loader: Any = None,
    keep_recent_messages: int = DEFAULT_KEEP_RECENT,
    focus: Optional[str] = None,
) -> AutoCompactResult:
    """用户主动压缩。强制触发并失效全部缓存。"""
    result = await maybe_auto_compact(
        messages,
        model=model,
        summarizer=summarizer,
        session_state=session_state,
        keep_recent_messages=keep_recent_messages,
        force=True,
        focus=focus,
    )
    # 即使 triggered=False(无可摘要内容),也清 section cache,让用户下一轮
    # 至少看到 "memory 重新加载" 的效果。
    clear_system_prompt_sections()
    if memory_loader is not None and hasattr(memory_loader, "reset_cache"):
        try:
            memory_loader.reset_cache(reason="user_compact")
        except Exception:
            logger.exception("memory_loader.reset_cache failed")
    return result


__all__ = ["compact_now"]
