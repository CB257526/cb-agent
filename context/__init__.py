"""cb-agent 上下文工程公共入口。

模块只保留 prompt 组装、记忆加载和上下文预算。compact 运行时由 agent 层统一
管理，provider 前缀缓存由稳定的请求顺序保证。
"""

from .budget import ContextPercentages, calculate_context_percentages, count_tokens
from .budget.window import get_context_window_for_model
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
    "ContextPercentages",
    "KnowledgeBase",
    "KnowledgeCaptureResult",
    "MemoryFileInfo",
    "MemoryLoader",
    "MemoryType",
    "UserContext",
    "build_effective_system_prompt",
    "calculate_context_percentages",
    "count_tokens",
    "format_memory_files",
    "get_context_window_for_model",
    "get_dynamic_context_prompt",
    "get_dynamic_context_sections",
    "get_static_system_prompt",
    "get_system_prompt",
    "get_user_context",
    "invalidate_user_context",
]
