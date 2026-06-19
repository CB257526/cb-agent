"""上下文工程模块。

按 Section / Memory / Compaction / Budget 组织:

- sections/: SystemPromptSection 注册表 + LRU 缓存 + 静态/动态段
- prompts/:  Chat Completions 静态 system 与动态 context 组装入口
- memory/:   多级 CLAUDE.md 加载 + @include 递归 + frontmatter
- compact/:  自动/用户压缩 + boundary marker
- budget/:   上下文窗口推断 + token 计数

旧的 GSSC 流水线(ContextBuilder/ContextPacket/ContextPriority)已删除。
"""

from .budget import (
    ContextPercentages,
    calculate_context_percentages,
    count_tokens,
)
from .budget.window import get_context_window_for_model
from .compact import (
    AutoCompactResult,
    RuleBasedSummarizer,
    Summarizer,
    compact_now,
    find_last_compact_boundary,
    is_compact_boundary,
    make_compact_boundary_message,
    maybe_auto_compact,
    messages_after_last_boundary,
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
    get_static_system_prompt,
    get_system_prompt,
)
from .sections import (
    SystemPromptSection,
    SystemPromptSectionCache,
    clear_system_prompt_sections,
    get_system_prompt_section_cache,
)
from .system_prompt import build_effective_system_prompt
from .user_context import (
    UserContext,
    get_user_context,
    invalidate_user_context,
)

__all__ = [
    # budget
    "ContextPercentages",
    "calculate_context_percentages",
    "count_tokens",
    "get_context_window_for_model",
    # compact
    "AutoCompactResult",
    "RuleBasedSummarizer",
    "Summarizer",
    "compact_now",
    "find_last_compact_boundary",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "maybe_auto_compact",
    "messages_after_last_boundary",
    # memory
    "KnowledgeBase",
    "KnowledgeCaptureResult",
    "MemoryFileInfo",
    "MemoryLoader",
    "MemoryType",
    "format_memory_files",
    # prompts
    "get_dynamic_context_prompt",
    "get_static_system_prompt",
    "get_system_prompt",
    # sections
    "SystemPromptSection",
    "SystemPromptSectionCache",
    "clear_system_prompt_sections",
    "get_system_prompt_section_cache",
    # top-level
    "UserContext",
    "build_effective_system_prompt",
    "get_user_context",
    "invalidate_user_context",
]
