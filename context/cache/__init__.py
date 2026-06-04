"""cache 子模块 —— prompt cache scope 抽象与 provider 适配。"""

from .blocks import build_system_prompt_blocks
from .provider_adapter import (
    AnthropicAdapter,
    CacheControlAdapter,
    OpenAICompatibleAdapter,
)
from .scope import CacheScope, SystemPromptBlock, should_use_global_cache_scope
from .split import split_sys_prompt_prefix

__all__ = [
    "AnthropicAdapter",
    "CacheControlAdapter",
    "CacheScope",
    "OpenAICompatibleAdapter",
    "SystemPromptBlock",
    "build_system_prompt_blocks",
    "should_use_global_cache_scope",
    "split_sys_prompt_prefix",
]
