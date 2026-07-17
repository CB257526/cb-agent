"""cb-agent 上下文工程公共入口。

模块只保留四类职责：prompt 组装、记忆加载、上下文预算和 compact replacement
history。provider 前缀缓存由稳定的请求顺序保证，不在本地维护第二套缓存系统。
"""

from .budget import ContextPercentages, calculate_context_percentages, count_tokens
from .budget.window import get_context_window_for_model
from .compact import (
    COMPACT_BOUNDARY_KIND,
    COMPACT_BOUNDARY_PREFIX,
    CompactionSelection,
    DEFAULT_RETAINED_MESSAGE_TOKENS,
    estimate_messages_tokens,
    find_last_compact_boundary,
    find_last_compact_boundary_index,
    get_messages_after_compact_boundary,
    has_meaningful_summary_source,
    is_compact_boundary,
    make_compact_boundary_message,
    messages_after_last_boundary,
    select_compaction_history,
)
from .memory import (
    KnowledgeBase,
    KnowledgeCaptureResult,
    MemoryFileInfo,
    MemoryLoader,
    MemoryType,
    format_memory_files,
)
from .prompts import (
    get_dynamic_context_prompt,
    get_dynamic_context_sections,
    get_static_system_prompt,
    get_system_prompt,
)
from .system_prompt import build_effective_system_prompt
from .user_context import UserContext, get_user_context, invalidate_user_context

__all__ = [
    "COMPACT_BOUNDARY_KIND",
    "COMPACT_BOUNDARY_PREFIX",
    "CompactionSelection",
    "ContextPercentages",
    "DEFAULT_RETAINED_MESSAGE_TOKENS",
    "KnowledgeBase",
    "KnowledgeCaptureResult",
    "MemoryFileInfo",
    "MemoryLoader",
    "MemoryType",
    "UserContext",
    "build_effective_system_prompt",
    "calculate_context_percentages",
    "count_tokens",
    "estimate_messages_tokens",
    "find_last_compact_boundary",
    "find_last_compact_boundary_index",
    "format_memory_files",
    "get_context_window_for_model",
    "get_dynamic_context_prompt",
    "get_dynamic_context_sections",
    "get_messages_after_compact_boundary",
    "get_static_system_prompt",
    "get_system_prompt",
    "get_user_context",
    "has_meaningful_summary_source",
    "invalidate_user_context",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "messages_after_last_boundary",
    "select_compaction_history",
]
