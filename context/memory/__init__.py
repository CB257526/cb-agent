"""Memory 子模块 —— CLAUDE.md 多级加载与组装。

对应 claude-code/src/utils/claudemd.ts。
"""

from .formatter import MEMORY_INSTRUCTION_PROMPT, format_memory_files
from .knowledge import KnowledgeBase, KnowledgeCaptureResult
from .loader import MemoryLoader
from .types import MemoryFileInfo, MemoryType

__all__ = [
    "KnowledgeBase",
    "KnowledgeCaptureResult",
    "MEMORY_INSTRUCTION_PROMPT",
    "MemoryFileInfo",
    "MemoryLoader",
    "MemoryType",
    "format_memory_files",
]
