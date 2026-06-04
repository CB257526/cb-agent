"""build_system_prompt_blocks —— 主入口,把 list[str] 一步切成 block 列表。

对应 claude-code/src/services/api/claude.ts:3352 的 buildSystemPromptBlocks。

cb-agent 当前主走 OpenAI 兼容 API,所以 emit 由 provider_adapter 处理。
本模块只负责"切分 + scope 标注",不负责具体 API 格式。
"""

from __future__ import annotations

from typing import List, Sequence

from .scope import SystemPromptBlock
from .split import split_sys_prompt_prefix


def build_system_prompt_blocks(
    system_prompt: Sequence[str],
    *,
    use_global_cache_scope: bool = False,
) -> List[SystemPromptBlock]:
    """把 list[str] 形式的 system prompt 切成 SystemPromptBlock 列表。

    use_global_cache_scope:
        True  -> 路径 A,SYSTEM_PROMPT_DYNAMIC_BOUNDARY 前后分两块。仅当
                 你的 provider 支持 Anthropic-style global cache scope 时使用。
        False -> 路径 B,整体一个 ORG 段。OpenAI 兼容 API 默认走这条。
    """
    return split_sys_prompt_prefix(
        system_prompt,
        use_global_cache_scope=use_global_cache_scope,
    )


__all__ = ["build_system_prompt_blocks"]
