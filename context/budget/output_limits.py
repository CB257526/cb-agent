"""输出 token 上限相关常量。

对应 claude-code/src/utils/context.ts 中的 max output tokens 常量集。

- CAPPED_DEFAULT_MAX_TOKENS: 默认输出预留(slot 友好,8K)
- ESCALATED_MAX_TOKENS: 触顶后自动升级 (64K)
- COMPACT_MAX_OUTPUT_TOKENS: 压缩摘要请求专用上限 (20K)

cb-agent 当前不主动 escalate,但保留常量供 session/compact 模块引用,
让阈值统一可调。
"""

from __future__ import annotations

CAPPED_DEFAULT_MAX_TOKENS = 8_000  # 默认输出预留(slot 友好,8K)
ESCALATED_MAX_TOKENS = 64_000  # 触顶后自动升级 (64K)
COMPACT_MAX_OUTPUT_TOKENS = 20_000  # 压缩摘要请求专用上限 (20K)


def get_max_output_tokens_for_model(
    model: str,
    *,
    escalate: bool = False,
) -> int:
    """返回该 model 的输出 max_tokens。

    escalate=True 时返回 ESCALATED_MAX_TOKENS;否则返回 default。
    具体 model 上限校验由 LLM 客户端层负责,这里只给推荐值。
    """
    del model
    return ESCALATED_MAX_TOKENS if escalate else CAPPED_DEFAULT_MAX_TOKENS


__all__ = [
    "CAPPED_DEFAULT_MAX_TOKENS",
    "ESCALATED_MAX_TOKENS",
    "COMPACT_MAX_OUTPUT_TOKENS",
    "get_max_output_tokens_for_model",
]
