"""BashTool 命令分类与工具函数

参考 Claude Code utils.ts + BashTool.tsx 中 isSearchOrReadBashCommand / isSilentBashCommand 的逻辑。

负责：命令分类、分类查找、shell 选择。
"""

import os
import platform
import shutil
import subprocess
from typing import Dict, List, Optional, Set

# ========== 命令分类集 ==========

SEARCH_COMMANDS: Set[str] = {"find", "grep", "rg", "ag", "locate", "which", "whereis"}
READ_COMMANDS:   Set[str] = {"cat", "head", "tail", "wc", "stat", "file", "jq", "awk", "cut", "sort", "uniq", "tr", "strings"}
LIST_COMMANDS:   Set[str] = {"ls", "tree", "du", "dir"}
SILENT_COMMANDS: Set[str] = {
    "mv", "cp", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp",
    "touch", "ln", "cd", "export", "unset", "wait",
}

SEMANTIC_NEUTRAL_COMMANDS: Set[str] = {"echo", "printf", "true", "false", ":"}

IS_WINDOWS = platform.system() == "Windows"


def classify_command(command: str) -> Dict[str, str]:
    """解析命令类型：search / read / list / silent / normal。"""
    tokens = command.strip().split()
    if not tokens:
        return {"kind": "normal"}
    for token in tokens:
        if token in ("&&", "||", "|", ";", ">", ">>", "<"):
            continue
        if "=" in token and not token.startswith("-"):
            continue
        base = token.split("/")[-1]
        if base in SEARCH_COMMANDS:  return {"kind": "search"}
        if base in READ_COMMANDS:    return {"kind": "read"}
        if base in LIST_COMMANDS:    return {"kind": "list"}
        if base in SILENT_COMMANDS:  return {"kind": "silent"}
        return {"kind": "normal"}
    return {"kind": "normal"}


# ========== Shell 自动发现 ==========

_shell_cache: Optional[Dict] = None


def _detect_shell() -> Dict:
    """自动检测当前 Windows 系统可用的最佳 shell。

    优先级：PowerShell > Git Bash > WSL bash > cmd.exe
    结果缓存，进程内只跑一次。
    """
    global _shell_cache
    if _shell_cache is not None:
        return _shell_cache

    if not IS_WINDOWS:
        _shell_cache = {"kind": "bash", "command": ["bash", "-c"], "wrap": _noop_wrap}
        return _shell_cache

    # 1. PowerShell — 所有现代 Windows 自带
    ps_path = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if ps_path:
        _shell_cache = {
            "kind": "powershell",
            "command": [ps_path, "-NoProfile", "-Command"],
            "wrap": _ps_wrap,
        }
        return _shell_cache

    # 2. Git Bash — 随 Git for Windows 安装
    git_bash = r"C:\Program Files\Git\bin\bash.exe"
    if os.path.isfile(git_bash):
        _shell_cache = {"kind": "git-bash", "command": [git_bash, "-c"], "wrap": _noop_wrap}
        return _shell_cache

    # 3. WSL
    wsl = shutil.which("wsl.exe")
    if wsl:
        _shell_cache = {"kind": "wsl", "command": [wsl, "bash", "-c"], "wrap": _noop_wrap}
        return _shell_cache

    # 4. 降级到 cmd.exe（chcp 65001 占位，实际在 wrap 里拼）
    _shell_cache = {"kind": "cmd", "command": ["cmd.exe", "/c"], "wrap": _cmd_wrap}
    return _shell_cache


def _noop_wrap(command: str) -> str:
    return command


def _ps_wrap(command: str) -> str:
    """PowerShell 包装：把命令包在双引号里传给 -Command。

    ps 内部双引号需转义；简单替换 \" → `\" 覆盖大部分场景。
    """
    return command


def _cmd_wrap(command: str) -> str:
    """cmd.exe 包装：先 chcp 65001 切换到 UTF-8 代码页再执行。"""
    return f"chcp 65001 > nul && {command}"


def get_shell() -> List[str]:
    """跨平台选 shell 解释器。"""
    return _detect_shell()["command"]


def wrap_command(command: str) -> str:
    """Shell 特定的命令包装（编码修正、转义等）。"""
    return _detect_shell()["wrap"](command)


def get_platform_hint() -> str:
    """当前平台的 shell 提示，用于注入给模型。"""
    if not IS_WINDOWS:
        return ""
    kind = _detect_shell()["kind"]
    if kind == "powershell":
        return (
            "Shell 运行在 **Windows PowerShell**。"
            "可以使用大部分 Unix 命令（`ls`、`cat`、`curl`、`rm`、`mkdir` 等），"
            "它们在 PS 中是内置 cmdlet 或 alias。"
            "中文路径直接传，编码自动处理。"
        )
    elif kind == "wsl":
        return "Shell 运行在 **WSL bash**。标准的 Linux 环境。"
    elif kind == "git-bash":
        return "Shell 运行在 **Git Bash**。标准的 Unix 命令都可用。"
    else:
        return (
            "Shell 运行在 **Windows cmd.exe**。"
            "注意：`cat` → `type`，`ls` → `dir`，`grep` → `findstr`。"
        )
