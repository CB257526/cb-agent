"""Skills 包

为 Agent 提供 Skill 管理能力，支持从 .cbagent/skills/ 目录发现、解析和加载 Skill。

v2 新增特性:
- 条件激活 (paths frontmatter)
- 预算控制 L1 概览
- 使用频率追踪与排序
- 别名支持
- 热重载
"""

from .skill import Skill
from .skill_manager import SkillManager
from .skill_executor import SkillExecutor

__all__ = ["Skill", "SkillManager", "SkillExecutor"]
