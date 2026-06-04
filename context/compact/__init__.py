"""compact 子模块 —— 自动 / 用户触发 / 客户端模拟 三层压缩。

对应 claude-code/src/services/compact/。
"""

from .auto_compact import (
    AutoCompactResult,
    DEFAULT_KEEP_RECENT,
    DEFAULT_THRESHOLD_PCT,
    maybe_auto_compact,
)
from .boundary import (
    COMPACT_BOUNDARY_KIND,
    find_last_compact_boundary,
    is_compact_boundary,
    make_compact_boundary_message,
    messages_after_last_boundary,
)
from .cached_microcompact import (
    CachedMCState,
    maybe_microcompact_tool_results,
)
from .compact import compact_now
from .session_memory_compact import try_session_memory_summary
from .summarizer import RuleBasedSummarizer, Summarizer

__all__ = [
    "AutoCompactResult",
    "COMPACT_BOUNDARY_KIND",
    "CachedMCState",
    "DEFAULT_KEEP_RECENT",
    "DEFAULT_THRESHOLD_PCT",
    "RuleBasedSummarizer",
    "Summarizer",
    "compact_now",
    "find_last_compact_boundary",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "maybe_auto_compact",
    "maybe_microcompact_tool_results",
    "messages_after_last_boundary",
    "try_session_memory_summary",
]
