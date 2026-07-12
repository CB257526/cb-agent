"""内置子代理角色列表。

每个角色放在独立文件中。新增角色时只需增加模块并把定义加入
``BUILTIN_SUBAGENTS``，运行时和工具层无需改动。
"""

from .explore import EXPLORE_SUBAGENT
from .general import GENERAL_SUBAGENT
from .reviewer import REVIEWER_SUBAGENT
from .worker import WORKER_SUBAGENT


BUILTIN_SUBAGENTS = (
    GENERAL_SUBAGENT,
    EXPLORE_SUBAGENT,
    REVIEWER_SUBAGENT,
    WORKER_SUBAGENT,
)


__all__ = [
    "BUILTIN_SUBAGENTS",
    "EXPLORE_SUBAGENT",
    "GENERAL_SUBAGENT",
    "REVIEWER_SUBAGENT",
    "WORKER_SUBAGENT",
]
