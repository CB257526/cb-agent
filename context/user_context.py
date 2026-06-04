"""UserContext —— 运行环境快照(memoize 一次进程)。

对应 claude-code/src/context.ts:getUserContext。

UserContext 是 env_info Section 的数据源。session 切换 cwd 时调
invalidate_user_context() 让快照失效。
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


@dataclass(frozen=True)
class UserContext:
    """一份进程级运行环境快照。"""

    cwd: Path
    is_git: bool
    platform: str
    shell: str
    additional_directories: Tuple[Path, ...] = ()


@functools.lru_cache(maxsize=1)
def _compute() -> UserContext:
    from .prompts.env_info import _detect_platform, _detect_shell, _is_git_repo

    cwd = Path.cwd().resolve()
    return UserContext(
        cwd=cwd,
        is_git=_is_git_repo(cwd),
        platform=_detect_platform(),
        shell=_detect_shell(),
        additional_directories=(),
    )


def get_user_context(cwd: Optional[Path] = None) -> UserContext:
    """返回当前进程的运行环境快照。

    cwd 可选;传入时强制重算并不缓存(用于多 session 场景)。
    """
    if cwd is not None:
        from .prompts.env_info import _detect_platform, _detect_shell, _is_git_repo

        cwd = cwd.resolve()
        return UserContext(
            cwd=cwd,
            is_git=_is_git_repo(cwd),
            platform=_detect_platform(),
            shell=_detect_shell(),
            additional_directories=(),
        )
    return _compute()


def invalidate_user_context() -> None:
    """让 memoize 失效。session 切换或环境变化时调。"""
    _compute.cache_clear()


__all__ = ["UserContext", "get_user_context", "invalidate_user_context"]
