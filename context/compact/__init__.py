"""上下文压缩的 boundary 与 replacement history 选择。"""

from .boundary import (
    COMPACT_BOUNDARY_KIND,
    COMPACT_BOUNDARY_PREFIX,
    find_last_compact_boundary,
    find_last_compact_boundary_index,
    get_messages_after_compact_boundary,
    is_compact_boundary,
    make_compact_boundary_message,
    messages_after_last_boundary,
)
from .history import (
    CompactionSelection,
    DEFAULT_RETAINED_MESSAGE_TOKENS,
    estimate_messages_tokens,
    has_meaningful_summary_source,
    select_compaction_history,
)

__all__ = [
    "COMPACT_BOUNDARY_KIND",
    "COMPACT_BOUNDARY_PREFIX",
    "CompactionSelection",
    "DEFAULT_RETAINED_MESSAGE_TOKENS",
    "estimate_messages_tokens",
    "find_last_compact_boundary",
    "find_last_compact_boundary_index",
    "get_messages_after_compact_boundary",
    "has_meaningful_summary_source",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "messages_after_last_boundary",
    "select_compaction_history",
]
