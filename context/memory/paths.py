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
CORE_MEMORY_FILENAMES = ("AGENT.md", "USER.md", "RULE.md", "MEMORY.md")
SHORT_TERM_MEMORY_NAME = "SHORT_TERM.md"
PROJECT_RULES_DIRS = (".claude/rules", ".cbagent/rules")
PROJECT_CLAUDE_SUBPATHS = (
    "AGENT.md",
    "USER.md",
    "RULE.md",
    "MEMORY.md",
    ".cbagent/AGENT.md",
    ".cbagent/USER.md",
    ".cbagent/RULE.md",
    ".cbagent/MEMORY.md",
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


def get_user_memory_dir() -> Path:
    """Return the user-global memory directory."""
    return Path.home() / ".cbagent"


def get_workspace_memory_dir() -> Path:
    """Return the memory workspace root.

    The user-facing default is ``~/``. Set ``CBAGENT_WORKSPACE_DIR`` to move
    the global memory files and knowledge directory together.
    """
    override = os.environ.get("CBAGENT_WORKSPACE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home().resolve()


def iter_user_core_memory_paths() -> Iterator[Path]:
    """Yield global memory files in stable load order.

    ``~/.cbagent/*.md`` is kept as a legacy location. ``~/AGENT.md`` /
    ``~/USER.md`` / ``~/RULE.md`` / ``~/MEMORY.md`` are loaded afterwards so
    the documented workspace-root files take precedence.
    """
    legacy_base = get_user_memory_dir()
    for name in CORE_MEMORY_FILENAMES:
        yield legacy_base / name
    base = get_workspace_memory_dir()
    for name in CORE_MEMORY_FILENAMES:
        yield base / name


def get_user_core_memory_path(name: str) -> Path:
    """Return the documented workspace-root core memory file path."""
    if name not in CORE_MEMORY_FILENAMES:
        raise ValueError(f"unsupported core memory file: {name}")
    return get_workspace_memory_dir() / name


def get_user_rules_dir() -> Path:
    """用户级 rules 目录。"""
    return Path.home() / ".cbagent" / "rules"


def get_short_term_memory_path(cwd: Path) -> Path:
    """Project-local short-term memory loaded after project memory."""
    return cwd.resolve() / ".cbagent" / SHORT_TERM_MEMORY_NAME


def get_knowledge_root(cwd: Path | None = None) -> Path:
    """Return the workspace knowledge root.

    Defaults to ``~/knowledge``. ``CBAGENT_KNOWLEDGE_DIR`` can point the agent
    at another workspace root without changing code.
    """
    override = os.environ.get("CBAGENT_KNOWLEDGE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (get_workspace_memory_dir() / "knowledge").resolve()


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
    "get_user_memory_dir",
    "get_workspace_memory_dir",
    "get_user_memory_path",
    "get_user_core_memory_path",
    "iter_user_core_memory_paths",
    "get_user_rules_dir",
    "get_short_term_memory_path",
    "get_knowledge_root",
    "get_local_memory_path",
    "iter_project_memory_candidates",
    "iter_rules_dir",
    "CORE_MEMORY_FILENAMES",
    "PROJECT_CLAUDE_NAME",
    "LOCAL_CLAUDE_NAME",
    "SHORT_TERM_MEMORY_NAME",
]
