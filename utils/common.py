"""通用工具函数。

count_tokens / tokenize_for_relevance / jaccard 是项目里多处共用的底层工具，
放在叶子模块 utils.common 里以避免循环 import。context.builder 反向引用本模块。
"""

from __future__ import annotations

import functools
import logging
from typing import FrozenSet, Optional

import tiktoken


logger = logging.getLogger(__name__)

_ENCODING_NAME = "cl100k_base"


@functools.lru_cache(maxsize=1)
def _get_encoding() -> "tiktoken.Encoding":
    """全局共享 tiktoken 编码器，进程内只初始化一次。

    这是性能关键路径：先前每次 count_tokens 都重新加载编码器，
    冷启 50-100ms，循环里调几十次会拖慢上层流程。
    """
    return tiktoken.get_encoding(_ENCODING_NAME)


def count_tokens(text: str, model_name: Optional[str] = None) -> int:  # noqa: ARG001
    """计算文本 token 数。

    model_name 参数仅为兼容旧签名而保留，实际被忽略；
    全局统一使用 cl100k_base 编码器（GPT-4 / GPT-4o 系列均适配）。
    异常时降级为 len(text)//4 粗估。
    """
    del model_name  # 显式标注：保留签名但不使用
    if not text:
        return 0
    try:
        return len(_get_encoding().encode(text))
    except Exception as e:  # 极少触发，仅做兜底
        logger.warning("token 计数失败，降级为字符估算: %s", e)
        return max(1, len(text) // 4)


@functools.lru_cache(maxsize=512)
def tokenize_for_relevance(text: str) -> FrozenSet[int]:
    """文本 -> token id frozenset，供相关性计算（Jaccard）使用。

    用 token id 而非字面词的好处：中文不需要分词、英文不需要 split，
    且与 count_tokens 共享同一个编码器，无重复初始化开销。
    """
    if not text:
        return frozenset()
    try:
        return frozenset(_get_encoding().encode(text))
    except Exception:
        return frozenset()


def jaccard(a: FrozenSet[int], b: FrozenSet[int]) -> float:
    """Jaccard 系数：|a ∩ b| / |a ∪ b|。任一为空返回 0。"""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


__all__ = [
    "count_tokens",
    "tokenize_for_relevance",
    "jaccard",
]
