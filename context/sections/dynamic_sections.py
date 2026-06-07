"""动态系统提示段 —— 每个函数返回一个 SystemPromptSection。

对应 claude-code/src/constants/prompts.ts 中放在 SYSTEM_PROMPT_DYNAMIC_BOUNDARY
之后的几段(memory / env_info / language / mcp_instructions / token_budget)。

设计要点:
- 每个 section 函数返回 SystemPromptSection,而不是直接返回字符串。
  resolve_system_prompt_sections 会把它们并发 resolve。
- compute 函数都是闭包,捕获了构造时的依赖(loader / model / settings)。
- mcp_instructions 用 DANGEROUS_uncached_*,因为 MCP 服务的 instructions
  字段在 connect/disconnect 之间会变,无稳定缓存键。
- 其余 section 用普通 system_prompt_section,缓存键即 section name。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from .registry import (
    DANGEROUS_uncached_system_prompt_section,
    SystemPromptSection,
    system_prompt_section,
)


def memory_section(memory_loader: Any) -> SystemPromptSection:
    """CLAUDE.md 多级合并段。

    memory_loader 是 MemoryLoader 实例(用 Any 类型避免循环 import)。
    compute 是 async,直接 await loader.get_memory_files() -> format_memory_files()。
    """
    async def compute() -> Optional[str]:
        from .. import memory as _memory_pkg

        files = await memory_loader.get_memory_files()
        if not files:
            return None
        return _memory_pkg.format_memory_files(files)

    return system_prompt_section("memory", compute)


def env_info_section(
    *,
    model: str,
    cwd: Optional[Path] = None,
    additional_directories: Optional[Sequence[Path]] = None,
) -> SystemPromptSection:
    """运行环境快照段。

    缓存键含 model_id;model 切换时会自动 miss。cwd / additional_dirs 切换
    需要外部 clear 缓存(session 切换时由 session 调 clear_system_prompt_sections)。
    """
    cwd_str = str((cwd or Path.cwd()).resolve())
    extras_str = ",".join(str(p) for p in (additional_directories or []))
    name = f"env_info::{model}::{cwd_str}::{extras_str}"

    def compute() -> str:
        from ..prompts.env_info import compute_env_info

        return compute_env_info(
            model=model,
            cwd=cwd,
            additional_directories=additional_directories,
        )

    return system_prompt_section(name, compute)


def current_time_section() -> SystemPromptSection:
    """当前时间元信息段。

    这一段必须每轮重算，不能塞进 ``env_info_section``。env_info 会按 model/cwd
    缓存，如果把时间放进去，模型看到的“今天”就可能停留在第一次组装 prompt 的
    时间点，进而在联网搜索或判断 latest/today 时继续使用过期年份。
    """

    def compute() -> str:
        local_now = datetime.now().astimezone()
        utc_now = datetime.now(timezone.utc)
        offset = local_now.strftime("%z")
        if len(offset) == 5:
            offset = f"{offset[:3]}:{offset[3:]}"

        return (
            "# Current time\n"
            f"- Current local datetime: {local_now.isoformat(timespec='seconds')}\n"
            f"- Current local date: {local_now.date().isoformat()}\n"
            f"- Current UTC datetime: {utc_now.isoformat(timespec='seconds')}\n"
            f"- Local UTC offset: {offset or 'unknown'}\n"
            "- Treat relative dates like today, yesterday, tomorrow, now, latest, and most recent "
            "as relative to this timestamp. For time-sensitive facts, verify current information "
            "instead of relying on stale training data."
        )

    return DANGEROUS_uncached_system_prompt_section(
        "current_time",
        compute,
        reason="The current timestamp changes every turn and stale cached time causes models to reason from old dates.",
    )


def language_section(language: Optional[str]) -> Optional[SystemPromptSection]:
    """用户语言偏好段。

    None 或空串时返回 None,该段不会注入。
    """
    if not language or not language.strip():
        return None
    name = f"language::{language.strip()}"

    def compute() -> str:
        return f"# Language\nThe user's preferred working language is {language.strip()}. Default to it for prose responses."

    return system_prompt_section(name, compute)


def mcp_instructions_section(mcp_clients: Optional[Sequence[Any]]) -> Optional[SystemPromptSection]:
    """MCP 服务的 instructions 字段聚合段。

    每次 connect/disconnect 都会改变内容,因此走 DANGEROUS_uncached_*
    每轮重算。reason 强制传入(显式说明缓存代价)。
    """
    if not mcp_clients:
        return None

    def compute() -> Optional[str]:
        chunks: list[str] = []
        for client in mcp_clients:
            instructions = getattr(client, "instructions", None) or getattr(
                client, "server_instructions", None
            )
            name = getattr(client, "name", None) or getattr(client, "server_name", "<mcp>")
            if instructions and isinstance(instructions, str) and instructions.strip():
                chunks.append(f"## MCP server `{name}`\n{instructions.strip()}")
        if not chunks:
            return None
        return "# MCP server instructions\n\n" + "\n\n".join(chunks)

    return DANGEROUS_uncached_system_prompt_section(
        "mcp_instructions",
        compute,
        reason="MCP servers can connect/disconnect between turns; their `instructions` field is per-server and not stably keyable.",
    )


def session_guidance_section(
    *,
    enabled_tools: frozenset[str],
    skill_commands: Optional[Sequence[Any]] = None,
) -> Optional[SystemPromptSection]:
    """每个 session 在工具/技能集合上的指导文字。

    示例: 当前启用工具列表、当注册了 ``bash`` 工具时提示 "Unix shell
    语法,不要用 Windows 命令";当注册了 skill 时,列出 /skill 调用方式。

    注意: 工具集合会随启动模式(memory_system、MCP、QQ transport 等)变化,因此
    这段刻意放在动态区,不要放回 static_sections,以免未来 provider prompt cache
    的静态前缀因为工具列表变化而失效。
    """
    skills_key = ",".join(sorted(getattr(s, "name", str(s)) for s in skill_commands or []))
    name = f"session_guidance::{','.join(sorted(enabled_tools))}::{skills_key}"

    def compute() -> Optional[str]:
        bits: list[str] = []
        if enabled_tools:
            bits.append(f"- Available tools: {', '.join(sorted(enabled_tools))}.")
        else:
            bits.append("- Available tools: (no tools registered).")
        if "bash" in enabled_tools:
            bits.append(
                "- The `bash` tool runs in a Unix-like shell even on Windows. Use forward "
                "slashes in paths and POSIX redirection (`>/dev/null`, not `>NUL`)."
            )
        if "file_read" in enabled_tools or "file_write" in enabled_tools or "file_edit" in enabled_tools:
            bits.append(
                "- Prefer `file_read` / `file_edit` / `file_write` for file I/O over invoking "
                "`cat` / `echo` through bash. Use `file_edit` for local replacements in existing files."
            )
        if skill_commands:
            names = ", ".join(
                sorted(getattr(s, "name", str(s)) for s in skill_commands)
            )
            bits.append(
                f"- Skills available: {names}. Invoke a skill by calling the `skill` tool "
                "with its name; the skill body is loaded lazily."
            )
        if not bits:
            return None
        return "# Session guidance\n" + "\n".join(bits)

    return system_prompt_section(name, compute)


def token_budget_section(*, budget_directive: Optional[str] = None) -> Optional[SystemPromptSection]:
    """用户传入 token 预算指令段。

    仅在用户显式给出 ``+500k`` / ``spend 2M tokens`` 这类指令时启用,
    否则该段不注入(避免 LLM 默认就被诱导产出长内容)。
    """
    if not budget_directive or not budget_directive.strip():
        return None
    name = f"token_budget::{budget_directive.strip()}"

    def compute() -> str:
        return (
            "# Token budget\n"
            f"User has set a token budget directive: `{budget_directive.strip()}`. "
            "Continue working until the budget is exhausted before declaring the task "
            "complete."
        )

    return system_prompt_section(name, compute)


__all__ = [
    "memory_section",
    "current_time_section",
    "env_info_section",
    "language_section",
    "mcp_instructions_section",
    "session_guidance_section",
    "token_budget_section",
]
