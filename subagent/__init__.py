"""cb-agent 子代理运行时。

本包只负责子代理定义、权限、生命周期和进度状态。LLM 会话与工具的具体装配
仍由 ``tools.tools.subagent_tool`` 完成，避免子代理核心反向依赖应用入口。
"""

from .manager import SubagentTaskManager
from .models import (
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_TYPE,
    SubagentDefinition,
    SubagentPermissionPolicy,
    SubagentTask,
)
from .registry import SubagentRegistry

__all__ = [
    "DEFAULT_SUBAGENT_MAX_TURNS",
    "DEFAULT_SUBAGENT_TYPE",
    "SubagentDefinition",
    "SubagentPermissionPolicy",
    "SubagentRegistry",
    "SubagentTask",
    "SubagentTaskManager",
]
