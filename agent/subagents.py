"""子代理旧导入路径兼容层。

新代码应从顶层 ``subagent`` 包导入。保留本模块是为了兼容已有插件、测试和用户
脚本，避免架构迁移同时制造无关的导入破坏。
"""

from __future__ import annotations

from typing import Any

from subagent.event_bridge import ScopedEventBus, make_subagent_completed, make_subagent_started
from subagent.manager import SubagentTaskManager
from subagent.models import (
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_TYPE,
    SubagentDefinition,
    SubagentTask,
)
from subagent.registry import SubagentRegistry


class SubagentTaskRegistry(SubagentTaskManager):
    """兼容旧名称和缺省父会话参数。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._legacy_notified: set[str] = set()

    def spawn(self, **kwargs: Any) -> SubagentTask:
        kwargs.setdefault("owner_session_id", "runtime-main")
        return super().spawn(**kwargs)

    def wait(self, task_id: str, timeout: float = 30.0, **kwargs: Any) -> Any:
        owner = str(kwargs.pop("owner_session_id", "runtime-main"))
        tasks = super().wait([task_id], owner_session_id=owner, timeout=timeout)
        return tasks[0] if tasks else None

    def kill(self, task_id: str, **kwargs: Any) -> Any:
        """兼容旧版 kill 名称，内部统一走可审计的 cancel 生命周期。"""

        owner = str(kwargs.pop("owner_session_id", "runtime-main"))
        return self.cancel(task_id, owner_session_id=owner)

    def drain_notifications(self) -> list[SubagentTask]:
        """兼容旧的一次性完成通知接口。"""

        completed = []
        for task in self.list():
            if task.is_terminal() and task.id not in self._legacy_notified:
                self._legacy_notified.add(task.id)
                completed.append(task)
        return completed


__all__ = [
    "DEFAULT_SUBAGENT_MAX_TURNS",
    "DEFAULT_SUBAGENT_TYPE",
    "ScopedEventBus",
    "SubagentDefinition",
    "SubagentRegistry",
    "SubagentTask",
    "SubagentTaskManager",
    "SubagentTaskRegistry",
    "make_subagent_completed",
    "make_subagent_started",
]
