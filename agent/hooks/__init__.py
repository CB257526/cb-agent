"""Agent hooks 子系统。

在 agent 生命周期关键点（工具执行前后、用户提交、会话开始、上下文压缩、收尾）
调用用户可配置的外部命令，命令可以阻止工具、改写工具输入或注入额外上下文。

设计上独立于 EventBus：EventBus 单向广播给前端，HookManager 双向收集决策影响
主流程；hook 触发时反过来用 EventBus emit HookStarted/HookCompleted 让前端可见。

详见 HOOKS_GUIDE.md。
"""

from __future__ import annotations

from .config import (
    DEFAULT_TIMEOUT,
    SUPPORTED_EVENTS,
    SUPPORTED_HANDLER_TYPES,
    HookGroup,
    HookHandler,
    HooksConfig,
    load_hooks_config,
)
from .manager import HookManager, HookOutcome
from .matcher import matches

__all__ = [
    "HookManager",
    "HookOutcome",
    "HooksConfig",
    "HookHandler",
    "HookGroup",
    "load_hooks_config",
    "matches",
    "SUPPORTED_EVENTS",
    "SUPPORTED_HANDLER_TYPES",
    "DEFAULT_TIMEOUT",
]
