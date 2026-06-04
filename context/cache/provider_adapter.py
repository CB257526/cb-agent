"""Provider adapter —— 把 SystemPromptBlock 转成具体 API 格式。

对应 claude-code 中 buildSystemPromptBlocks 输出后,根据 client 类型
(Anthropic SDK vs OpenAI SDK)采取不同 emit 策略。

cb-agent 默认走 OpenAICompatibleAdapter:把 block 列表 join 成单 string,
通过 OpenAI 协议的 ``messages[0].role=system`` 注入。AnthropicAdapter
预留接口,本次不实现具体 cache_control 注入。

收益(即使在 OpenAI 兼容路径下):
- Section LRU 缓存依旧避免重读 CLAUDE.md / 重算 env_info
- 调试 dump 仍可按 scope 着色
- 切回 Anthropic 直连时,session 端代码无需改动
"""

from __future__ import annotations

from typing import Any, List, Protocol, Sequence

from .scope import CacheScope, SystemPromptBlock


class CacheControlAdapter(Protocol):
    """provider 适配器协议。"""

    def emit_system(self, blocks: Sequence[SystemPromptBlock]) -> Any:
        """把 block 列表转成具体 API 期望的 system 字段格式。"""
        ...


class OpenAICompatibleAdapter:
    """OpenAI 兼容 API: blocks join 成单 string。

    SystemPromptBlock 的 scope 信息在这里被丢弃(provider 端不支持),
    但本地 SectionCache LRU 已经在 list[str] 层把 compute 成本省下来了。
    """

    def emit_system(self, blocks: Sequence[SystemPromptBlock]) -> str:
        return "\n\n".join(b.text for b in blocks if b.text)


class AnthropicAdapter:
    """Anthropic 原生 API: 返回 list[dict] 带 cache_control。

    本次不接入 Anthropic 客户端,这里仅占位。如果后续切换 provider,
    实现:
        for block in blocks:
            d = {"type": "text", "text": block.text}
            if block.scope == CacheScope.GLOBAL:
                d["cache_control"] = {"type": "ephemeral", "scope": "global"}
            elif block.scope == CacheScope.ORG:
                d["cache_control"] = {"type": "ephemeral"}
            result.append(d)
    并注意 Anthropic 限制 cache_control 标记最多 4 个 block。
    """

    def emit_system(self, blocks: Sequence[SystemPromptBlock]) -> List[dict]:
        out: List[dict] = []
        for block in blocks:
            if not block.text:
                continue
            entry: dict = {"type": "text", "text": block.text}
            if block.scope in (CacheScope.GLOBAL, CacheScope.ORG):
                entry["cache_control"] = {"type": "ephemeral"}
            out.append(entry)
        return out


__all__ = [
    "CacheControlAdapter",
    "OpenAICompatibleAdapter",
    "AnthropicAdapter",
]
