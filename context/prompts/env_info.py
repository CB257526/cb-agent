"""env_info Section —— 运行环境快照。

对应 claude-code/src/constants/prompts.ts 中的 computeSimpleEnvInfo。

输出格式参考 claude-code 但适配 cb-agent 的运行环境(Python + Windows 为主)。

为什么放在 dynamic 段(boundary 之后)而非 static:
- cwd / additional_directories 与会话相关
- is_git / git_root 取决于当前文件系统状态
- platform / shell 相对稳定但仍属于"运行时环境",不是身份声明的一部分
- 缓存键含 model_id,session 切换 cwd 时手动 clear 缓存
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Sequence


def _detect_shell() -> str:
    """识别当前 shell。Windows 下默认 'bash'(项目 CLAUDE.md 要求 Unix 语法)。"""
    if os.name == "nt":
        # 用户的 CLAUDE.md 已经强制规定使用 bash 语法,不读 COMSPEC
        return "bash"
    return os.environ.get("SHELL", "/bin/sh")


def _detect_platform() -> str:
    """返回简短的 platform 标识。"""
    sys_name = platform.system()
    if sys_name == "Windows":
        try:
            release, version, csd, ptype = platform.win32_ver()
            return f"Windows {release}".strip() or "Windows"
        except Exception:
            return "Windows"
    if sys_name == "Darwin":
        return f"macOS {platform.mac_ver()[0]}".strip()
    return f"{sys_name} {platform.release()}".strip()


def _is_git_repo(cwd: Path) -> bool:
    """检查 cwd 是否在一个 git 仓库内。

    优先看 .git 目录(轻量),fallback 才调 git rev-parse。后者要起子进程,
    冷启动几十毫秒,放在 fallback 路径避免拖慢首次 system prompt 组装。
    """
    p = cwd
    while True:
        if (p / ".git").exists():
            return True
        parent = p.parent
        if parent == p:
            break
        p = parent
    # fallback: 当 .git 是 worktree submodule 链接文件时也能识别
    git_bin = shutil.which("git")
    if not git_bin:
        return False
    try:
        result = subprocess.run(
            [git_bin, "rev-parse", "--is-inside-work-tree"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:
        return False


def compute_env_info(
    *,
    model: str,
    cwd: Optional[Path] = None,
    additional_directories: Optional[Sequence[Path]] = None,
) -> str:
    """生成 env_info Section 文本。

    返回的字符串会作为一个 dynamic Section 注入 system prompt。
    缓存命中时 compute 不会重新调,所以 cwd/git 切换需要手动 clear 缓存。
    """
    cwd = (cwd or Path.cwd()).resolve()
    extras = list(additional_directories or [])
    is_git = _is_git_repo(cwd)
    platform_label = _detect_platform()
    shell = _detect_shell()

    lines = [
        "# Environment",
        f"- Working directory: {cwd}",
    ]
    if extras:
        lines.append(
            "- Additional directories: " + ", ".join(str(p) for p in extras)
        )
    lines.extend([
        f"- Is a git repository: {'yes' if is_git else 'no'}",
        f"- Platform: {platform_label}",
        f"- Shell: {shell}",
        f"- Model: {model}",
    ])
    return "\n".join(lines)


__all__ = ["compute_env_info"]
