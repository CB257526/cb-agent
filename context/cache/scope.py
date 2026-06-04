"""CacheScope 与 SystemPromptBlock 数据结构。

对应 claude-code 中 cache_control 的 scope 概念。

CacheScope 三档:
- NONE:   不缓存(attribution header 与 dynamic 段)
- ORG:    单组织/单用户共享缓存(默认 path B —— 整体一个 ORG 段)
- GLOBAL: 跨组织共享缓存,仅 BOUNDARY 之前的纯静态内容才能用

OpenAI 兼容 API 没有 cache_control,但保留这个抽象的好处:
1. provider_adapter.OpenAICompatibleAdapter 简单 join 成单 string
2. 切到 Anthropic 直连时只换 adapter,上游零改动
3. 调试 dump 时能看到每段的预期缓存策略
4. SectionCache LRU 始终生效,跟 provider 无关
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class CacheScope(str, Enum):
    """对齐 Anthropic prompt cache 的 scope 字段。

    - NONE: 不打 cache_control,每次都全量发送。
    - ORG: 单组织/单用户级 cache,Anthropic 默认范围。
    - GLOBAL: 跨组织共享 cache,仅 SDK 服务端纯静态片段适用。
    """

    NONE = "none"
    ORG = "org"
    GLOBAL = "global"


@dataclass
class SystemPromptBlock:
    """system prompt 的一个分块,带 scope 标记。

    text 已经是发送给 LLM 的最终字符串(同 scope 相邻段会被预先 join)。
    cache_scope 决定 provider_adapter 怎么 emit。
    """

    text: str
    scope: CacheScope


def should_use_global_cache_scope(model: str) -> bool:
    """是否对该 model 启用 global cache scope。

    OpenAI 兼容 API 不支持,直接返回 False。Anthropic 原生模型且开启
    1h cache TTL 的场景才返回 True(本次先返回 False,留给后续接入)。

    放在 scope.py 而非 blocks.py 是为了让 prompts/builder.py 可以独立
    判断是否插入 SYSTEM_PROMPT_DYNAMIC_BOUNDARY,不依赖 blocks(避免
    builder ↔ blocks 之间的循环导入)。
    """
    del model
    return False


__all__ = ["CacheScope", "SystemPromptBlock", "should_use_global_cache_scope"]
