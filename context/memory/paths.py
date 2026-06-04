"""Memory 路径优先级链。

对应 claude-code/src/utils/claudemd.ts 中的 getManagedMemoryPath /
getUserMemoryPath / 项目向上遍历逻辑。

设计要点:
- 加载顺序: Managed -> User -> Project(根->cwd 逐层) -> Local。数组靠后
  的文件在 system prompt 中位置靠后,模型更"重视"。
- 用户全局记忆放 ~/.cbagent/(与 Claude Code 的 ~/.claude/ 隔离),避免两个
  agent 共用同一份指令造成跨项目污染。
- 项目层每个父目录都查 4 个候选: CLAUDE.md / .claude/CLAUDE.md /
  .cbagent/CLAUDE.md / 以及 rules 目录下的 *.md。
- Managed 路径在 Windows 下走 %ProgramData%\\cb-agent\\,POSIX 走
  /etc/cb-agent/。两者都需要管理员权限才能写入,日常基本是空。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator


PROJECT_CLAUDE_NAME = "CLAUDE.md"
LOCAL_CLAUDE_NAME = "CLAUDE.local.md"
PROJECT_RULES_DIRS = (".claude/rules", ".cbagent/rules")
PROJECT_CLAUDE_SUBPATHS = (
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".cbagent/CLAUDE.md",
)


def get_managed_memory_path() -> Path:
    """全局管理员级记忆。Windows: %ProgramData%\\cb-agent\\CLAUDE.md。"""
    if os.name == "nt":
        program_data = os.environ.get("ProgramData") or "C:\\ProgramData"
        return Path(program_data) / "cb-agent" / "CLAUDE.md"
    return Path("/etc/cb-agent/CLAUDE.md")


def get_managed_rules_dir() -> Path:
    """全局管理员级 rules 目录。"""
    return get_managed_memory_path().parent / "rules"


def get_user_memory_path() -> Path:
    """用户全局记忆。~/.cbagent/CLAUDE.md。

    与 Claude Code 的 ~/.claude/CLAUDE.md 隔离,防止两个 agent 共用指令。
    """
    return Path.home() / ".cbagent" / "CLAUDE.md"


def get_user_rules_dir() -> Path:
    """用户级 rules 目录。"""
    return Path.home() / ".cbagent" / "rules"


def iter_project_memory_candidates(cwd: Path) -> Iterator[tuple[Path, str]]:
    """从根目录向 cwd 逐层 yield 候选 (memory_path, label)。

    label 用于调试/日志,真实 type 在 loader 中根据来源决定。
    cwd 越远(越靠根)的越先 yield -> 数组里位置靠前 -> 优先级更低。
    cwd 自身的项目级文件最后 yield -> 优先级最高。
    """
    cwd = cwd.resolve()
    chain: list[Path] = []
    p = cwd
    while True:
        chain.append(p)
        parent = p.parent
        if parent == p:
            break
        p = parent
    # 根 -> cwd 顺序: 反转
    for directory in reversed(chain):
        for sub in PROJECT_CLAUDE_SUBPATHS:
            candidate = directory / sub
            if candidate.is_file():
                yield candidate, f"project:{sub}"
        for rules_sub in PROJECT_RULES_DIRS:
            rules_dir = directory / rules_sub
            if rules_dir.is_dir():
                for md in sorted(rules_dir.glob("*.md")):
                    if md.is_file():
                        yield md, f"project:{rules_sub}/{md.name}"


def iter_rules_dir(rules_dir: Path) -> Iterator[Path]:
    """yield rules 目录下所有 *.md(按文件名排序,稳定)。"""
    if not rules_dir.is_dir():
        return
    for md in sorted(rules_dir.glob("*.md")):
        if md.is_file():
            yield md


def get_local_memory_path(cwd: Path) -> Path:
    """项目本地私有记忆。CLAUDE.local.md 不入版本控制,仅本机生效。"""
    return cwd.resolve() / LOCAL_CLAUDE_NAME


__all__ = [
    "get_managed_memory_path",
    "get_managed_rules_dir",
    "get_user_memory_path",
    "get_user_rules_dir",
    "get_local_memory_path",
    "iter_project_memory_candidates",
    "iter_rules_dir",
    "PROJECT_CLAUDE_NAME",
    "LOCAL_CLAUDE_NAME",
]
