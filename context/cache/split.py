"""splitSysPromptPrefix —— 把 list[str] 切成带 scope 的 SystemPromptBlock。

对应 claude-code/src/utils/api.ts 中的 splitSysPromptPrefix。

切分逻辑:

Path A (use_global_cache_scope=True 且找到 BOUNDARY):
    BOUNDARY 之前所有段 -> CacheScope.GLOBAL (单一 block,join)
    BOUNDARY 之后所有段 -> CacheScope.NONE   (单一 block,join)

Path B (默认 / 找不到 BOUNDARY):
    所有段整体 -> CacheScope.ORG (单一 block)

Boundary marker 永远不出现在最终 block 文本中。
"""

from __future__ import annotations

from typing import List, Sequence

from ..prompts.boundary import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from .scope import CacheScope, SystemPromptBlock


def split_sys_prompt_prefix(
    system_prompt: Sequence[str],
    *,
    use_global_cache_scope: bool,
) -> List[SystemPromptBlock]:
    """切分 system prompt 为带 scope 的 block 列表。

    返回的 block 列表保证: 同 scope 的相邻段已合并为单一文本,便于
    Anthropic API "最多 4 个 cache_control" 限制下不浪费配额。
    """
    parts = [s for s in system_prompt if s and s.strip()]
    if not parts:
        return []

    if use_global_cache_scope and SYSTEM_PROMPT_DYNAMIC_BOUNDARY in parts:
        idx = parts.index(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
        static_text = "\n\n".join(parts[:idx])
        dynamic_text = "\n\n".join(parts[idx + 1:])
        blocks: List[SystemPromptBlock] = []
        if static_text:
            blocks.append(SystemPromptBlock(text=static_text, scope=CacheScope.GLOBAL))
        if dynamic_text:
            blocks.append(SystemPromptBlock(text=dynamic_text, scope=CacheScope.NONE))
        return blocks

    # Path B: 整体 ORG。先把 boundary marker 滤掉(防御性,正常不应到这里)
    safe_parts = [p for p in parts if p != SYSTEM_PROMPT_DYNAMIC_BOUNDARY]
    if not safe_parts:
        return []
    return [SystemPromptBlock(text="\n\n".join(safe_parts), scope=CacheScope.ORG)]


__all__ = ["split_sys_prompt_prefix"]
