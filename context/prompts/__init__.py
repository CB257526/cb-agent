"""Prompt 组装子模块。

对应 claude-code/src/constants/prompts.ts —— get_system_prompt() 主入口。
"""

from .boundary import SYSTEM_PROMPT_DYNAMIC_BOUNDARY
from .builder import get_system_prompt
from .env_info import compute_env_info

__all__ = [
    "SYSTEM_PROMPT_DYNAMIC_BOUNDARY",
    "compute_env_info",
    "get_system_prompt",
]
