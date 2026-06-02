"""上下文工程模块

为 cb-agent 框架提供上下文工程能力：
- ContextBuilder: GSSC 流水线（Gather-Select-Structure-Compress），同步与异步双入口
- ContextConfig:  构建配置（预算、相关性权重、MMR、检索参数等）
- ContextPacket:  上下文片段
- ContextPriority: 优先级枚举（P0_SYSTEM/P1_STATE/P2_EVIDENCE/P3_HISTORY）
- ContextResult:  详细结果（含被丢弃片段、是否截断等）
- MarkdownMemoryProvider: 默认轻量 Markdown 记忆 provider
- count_tokens / messages_to_text: 工具函数
"""

from .builder import (
    ContextBuilder,
    ContextConfig,
    ContextPacket,
    ContextPriority,
    ContextResult,
    count_tokens,
    messages_to_text,
)
from .markdown_memory import (
    MarkdownMemoryItem,
    MarkdownMemoryProvider,
    MarkdownMemoryResult,
    default_global_memory_dir,
    default_project_memory_dir,
)

__all__ = [
    "ContextBuilder",
    "ContextConfig",
    "ContextPacket",
    "ContextPriority",
    "ContextResult",
    "MarkdownMemoryItem",
    "MarkdownMemoryProvider",
    "MarkdownMemoryResult",
    "count_tokens",
    "default_global_memory_dir",
    "default_project_memory_dir",
    "messages_to_text",
]
