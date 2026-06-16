"""上下文窗口大小推断。

对应 claude-code/src/utils/context.ts:getContextWindowForModel。

优先级:
1. env MAX_TOKENS(支持 1024K / 1M 写法,与 ConstantLLM 同一个键)
2. env override CB_AGENT_MAX_CONTEXT_TOKENS(历史兼容键,纯数字)
3. model id 含 "[1m]" / "[1M]" / "1m" 后缀 -> 1_000_000
4. ConstantLLM.llm_dict[model]['max_tokens'] (现有字段)
5. betas 含 "context-1m-2025-08-07" -> 1_000_000
6. DEFAULT_CONTEXT_WINDOW = 200_000

第 1 条让换服务商后模型名对不上 llm_dict 时,仍能用 .env 的 MAX_TOKENS 统一
指定窗口——和 session.py 主链路走的 ConstantLLM.model_max_tokens 共用同一个键,
两条窗口路径不再各读各的。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional, Sequence


logger = logging.getLogger(__name__)


DEFAULT_CONTEXT_WINDOW = 200_000
ENV_OVERRIDE_KEY = "CB_AGENT_MAX_CONTEXT_TOKENS"
CONTEXT_1M_BETA_HEADER = "context-1m-2025-08-07"
_1M_RE = re.compile(r"\[(?:1m|1M)\]|(?<![A-Za-z0-9])1[mM](?![A-Za-z0-9])")


def _read_max_tokens_env() -> Optional[int]:
    """读 MAX_TOKENS(支持 K/M 后缀),复用 ConstantLLM 的解析逻辑保持一致。"""
    try:
        from constant.llm.constant_llm import _parse_token_count_env, ConstantLLM
    except Exception:
        return None
    return _parse_token_count_env(os.getenv(ConstantLLM.ENV_MAX_TOKENS))


def _read_env_override() -> Optional[int]:
    raw = os.environ.get(ENV_OVERRIDE_KEY)
    if not raw:
        return None
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        logger.warning("invalid %s=%r ignored", ENV_OVERRIDE_KEY, raw)
        return None
    if value <= 0:
        return None
    return value


def _has_1m_suffix(model: str) -> bool:
    return bool(_1M_RE.search(model))


def _read_registry(model: str) -> Optional[int]:
    """从 ConstantLLM.llm_dict 读 max_tokens。"""
    try:
        from constant.llm.constant_llm import ConstantLLM
    except Exception:
        return None
    config = ConstantLLM.llm_dict.get(model)
    if not isinstance(config, dict):
        return None
    raw = config.get("max_input_tokens") or config.get("max_tokens")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def get_context_window_for_model(
    model: str,
    *,
    betas: Optional[Sequence[str]] = None,
) -> int:
    """六级优先级返回上下文窗口大小(tokens)。"""
    if not model:
        # 即便没有 model,显式的 MAX_TOKENS env 仍应生效。
        return _read_max_tokens_env() or DEFAULT_CONTEXT_WINDOW
    max_tokens_env = _read_max_tokens_env()
    if max_tokens_env is not None:
        return max_tokens_env
    env_value = _read_env_override()
    if env_value is not None:
        return env_value
    if _has_1m_suffix(model):
        return 1_000_000
    registry = _read_registry(model)
    if registry is not None:
        return registry
    if betas and CONTEXT_1M_BETA_HEADER in betas:
        return 1_000_000
    return DEFAULT_CONTEXT_WINDOW


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "CONTEXT_1M_BETA_HEADER",
    "get_context_window_for_model",
]
