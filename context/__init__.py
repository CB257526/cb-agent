"""上下文工程模块 —— 对齐 Claude Code 的设计。

按 Section / Boundary / Memory / Compaction 四个核心抽象组织:

- sections/: SystemPromptSection 注册表 + LRU 缓存 + 静态/动态段
- prompts/:  组装入口 get_system_prompt + SYSTEM_PROMPT_DYNAMIC_BOUNDARY
- memory/:   多级 CLAUDE.md 加载 + @include 递归 + frontmatter
- compact/:  自动/用户/客户端模拟三层压缩 + boundary marker
- cache/:    CacheScope / SystemPromptBlock + provider adapter
- budget/:   上下文窗口推断 + token 计数

旧的 GSSC 流水线(ContextBuilder/ContextPacket/ContextPriority)已删除。
"""

from .budget import (
    ContextPercentages,
    calculate_context_percentages,
    count_tokens,
)
from .budget.window import get_context_window_for_model
from .cache import (
    AnthropicAdapter,
    CacheControlAdapter,
    CacheScope,
    OpenAICompatibleAdapter,
    SystemPromptBlock,
    build_system_prompt_blocks,
    should_use_global_cache_scope,
)
from .compact import (
    AutoCompactResult,
    CachedMCState,
    RuleBasedSummarizer,
    Summarizer,
    compact_now,
    find_last_compact_boundary,
    is_compact_boundary,
    make_compact_boundary_message,
    maybe_auto_compact,
    maybe_microcompact_tool_results,
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
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
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
    # cache
    "AnthropicAdapter",
    "CacheControlAdapter",
    "CacheScope",
    "OpenAICompatibleAdapter",
    "SystemPromptBlock",
    "build_system_prompt_blocks",
    "should_use_global_cache_scope",
    # compact
    "AutoCompactResult",
    "CachedMCState",
    "RuleBasedSummarizer",
    "Summarizer",
    "compact_now",
    "find_last_compact_boundary",
    "is_compact_boundary",
    "make_compact_boundary_message",
    "maybe_auto_compact",
    "maybe_microcompact_tool_results",
    "messages_after_last_boundary",
    # memory
    "KnowledgeBase",
    "KnowledgeCaptureResult",
    "MemoryFileInfo",
    "MemoryLoader",
    "MemoryType",
    "format_memory_files",
    # prompts
    "SYSTEM_PROMPT_DYNAMIC_BOUNDARY",
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
