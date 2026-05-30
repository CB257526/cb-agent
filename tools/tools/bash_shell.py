"""Shell 检测、命令包装与平台提示

参考 Claude Code 的 shell 选择逻辑，按平台自动找到最合适的 shell：
Windows: PowerShell > Git Bash > WSL > cmd.exe
POSIX:   /bin/bash

修复了原 bash_utils.py 的两个 bug：
1. _ps_wrap 现在真正做 PowerShell 双引号转义并强制 UTF-8 OutputEncoding，
   否则 PS5 默认 cp936 会让中文输出乱码。
2. wrap_command 的产物现在会在 BashTool 里实际作为 shell 的参数使用
   （bash_tool.py 主调用点修复）。
"""

import os
import platform
import shutil
from typing import Dict, List, Optional


IS_WINDOWS = platform.system() == "Windows"

_shell_cache: Optional[Dict] = None


def _detect_shell() -> Dict:
    """检测当前系统可用的最佳 shell，结果缓存到进程退出。

    返回字典：
      {
        "kind":    "powershell" | "git-bash" | "wsl" | "cmd" | "bash",
        "command": [shell_exe, ...flags],   # 拼到 Popen 第一参数前面
        "wrap":    callable(command_str) -> wrapped_str,
      }
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

    # 4. 降级到 cmd.exe
    _shell_cache = {"kind": "cmd", "command": ["cmd.exe", "/c"], "wrap": _cmd_wrap}
    return _shell_cache


def _noop_wrap(command: str) -> str:
    """POSIX / Git Bash / WSL：命令不需要包装。"""
    return command


def _ps_wrap(command: str) -> str:
    """PowerShell 包装：注入 OutputEncoding=UTF8。

    注意：因为我们用 argv 形式传给 powershell.exe（-Command <整个字符串>），
    不需要二次转义引号——Popen 会处理 argv 边界。
    PS5 默认 OutputEncoding 是 cp936，不强制 UTF-8 中文输出会乱码。
    """
    prefix = (
        "$OutputEncoding = "
        "[Console]::OutputEncoding = "
        "[System.Text.Encoding]::UTF8; "
    )
    return prefix + command


def _cmd_wrap(command: str) -> str:
    """cmd.exe 包装：先 chcp 65001 切换到 UTF-8 代码页再执行。"""
    return f"chcp 65001 > nul && {command}"


def get_shell() -> List[str]:
    """跨平台选 shell 解释器（含必要 flags），用作 Popen 的 argv 前缀。"""
    return _detect_shell()["command"]


def wrap_command(command: str) -> str:
    """对原始命令做 shell 特定的包装（编码修正、转义等）。

    BashTool.run 必须把 wrap_command 的返回值传给 Popen，
    而不是原始 command；否则 _cmd_wrap / _ps_wrap 形同虚设。
    """
    return _detect_shell()["wrap"](command)


def get_shell_kind() -> str:
    """返回当前 shell 的种类标识，用于 prompt / 日志。"""
    return _detect_shell()["kind"]


def get_platform_hint() -> str:
    """当前平台的 shell 提示，用于注入给模型。"""
    if not IS_WINDOWS:
        return ""
    kind = _detect_shell()["kind"]
    if kind == "powershell":
        return (
            "Shell 运行在 **Windows PowerShell**。\n"
            "- 可以用大部分 Unix 命令别名（`ls`、`cat`、`pwd`、`rm`、`mkdir`），"
            "它们是 PS 的 cmdlet alias，中文路径直接传，编码自动处理。\n"
            "- **禁止使用 `&&` 和 `||`**：PowerShell 5 不支持这两个操作符。"
            "需要串接命令时：\n"
            "  - 不依赖前一条结果 → 用 `;` 串接，例如 `cd foo; ls`\n"
            "  - 依赖前一条结果 → 拆成多次 bash 调用（cb-agent 的 cwd 会跨调用持久化，不必担心 cd 状态丢失）\n"
            "  - 或者用 PowerShell 原生写法：`if ($?) { ... }`、`Get-... | ForEach-Object { ... }`"
        )
    elif kind == "wsl":
        return "Shell 运行在 **WSL bash**。标准的 Linux 环境。"
    elif kind == "git-bash":
        return "Shell 运行在 **Git Bash**。标准的 Unix 命令都可用。"
    else:
        return (
            "Shell 运行在 **Windows cmd.exe**。"
            "`cat` → `type`，`ls` → `dir`，`grep` → `findstr`。"
            "cmd 支持 `&&`，但同样建议用 cwd 持久化（cb-agent 跨调用记 cd）替代复杂串接。"
        )
