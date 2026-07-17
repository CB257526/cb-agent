"""Token 计数与上下文百分比计算。

count_tokens 从 utils.common 迁来,utils.common 改成 alias 这里的实现,
避免外部脚本(如 test_*.py)瞬间全断。

calculate_context_percentages 对应 claude-code 的 calculateContextPercentages。
返回 used / remaining 的整数百分比,供 TUI 状态栏与 auto_compact 触发条件共用。
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass
from typing import FrozenSet, Optional

import tiktoken


logger = logging.getLogger(__name__)

_ENCODING_NAME = "cl100k_base"


@functools.lru_cache(maxsize=1)
def _get_encoding() -> Optional["tiktoken.Encoding"]:
    """全局共享 tiktoken 编码器，并缓存离线初始化失败。

    性能关键路径:每次重建编码器冷启 50-100ms,循环里调几十次会拖慢上层流程。
    某些 tiktoken 安装会在首次加载时联网下载编码文件；离线失败后返回 None，
    让本进程后续统一走字符估算，避免每次计数都重复发起网络请求。
    """
    try:
        return tiktoken.get_encoding(_ENCODING_NAME)
    except Exception as exc:
        logger.warning("token 编码器初始化失败,本进程改用字符估算: %s", exc)
        return None


def count_tokens(text: str, model_name: Optional[str] = None) -> int:  # noqa: ARG001
    """计算文本 token 数。

    model_name 仅为兼容旧签名,实际被忽略;统一 cl100k_base 编码。
    异常时降级为 len(text)//4 粗估。
    """
    del model_name
    if not text:
        return 0
    encoding = _get_encoding()
    if encoding is None:
        return max(1, len(text) // 4)
    try:
        return len(encoding.encode(text))
    except Exception as e:
        logger.warning("token 计数失败,降级为字符估算: %s", e)
        return max(1, len(text) // 4)


@functools.lru_cache(maxsize=512)
def tokenize_for_relevance(text: str) -> FrozenSet[int]:
    """文本 -> token id frozenset,供相关性计算使用。

    保留是因为外部脚本(work_context.py 中的部分摘要逻辑)仍可能依赖。
    """
    if not text:
        return frozenset()
    encoding = _get_encoding()
    if encoding is None:
        return frozenset()
    try:
        return frozenset(encoding.encode(text))
    except Exception:
        return frozenset()


@dataclass(frozen=True)
class ContextPercentages:
    used: int
    remaining: int


def calculate_context_percentages(
    used_tokens: int,
    context_window_size: int,
) -> ContextPercentages:
    """计算上下文使用率。

    used_tokens 通常包含 input_tokens + cache_creation + cache_read。
    钳制到 [0, 100],避免估算误差导致 TUI 显示 -3% 或 102%。
    """
    if context_window_size <= 0:
        return ContextPercentages(used=0, remaining=100)
    raw = round(used_tokens / context_window_size * 100)
    used = min(100, max(0, raw))
    return ContextPercentages(used=used, remaining=100 - used)


__all__ = [
    "count_tokens",
    "tokenize_for_relevance",
    "calculate_context_percentages",
    "ContextPercentages",
]
