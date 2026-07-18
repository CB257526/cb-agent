"""通用工具函数 —— 重构后保留为 backward-compat alias 层。

count_tokens / tokenize_for_relevance 现在的实现位于 context.budget.tokens,
本模块仅做 import alias,保证旧测试与外部脚本不被瞬间打断。

GSSC 流水线移除后,jaccard 已无人调用,直接删除。如需相关性计算请改用
agent.compaction 或 work_context 自己的工具函数。
"""

from __future__ import annotations

from context.budget.tokens import count_tokens, tokenize_for_relevance


__all__ = [
    "count_tokens",
    "tokenize_for_relevance",
]
