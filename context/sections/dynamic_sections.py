"""运行时上下文文本生成函数。

本模块只负责把当前运行环境转换为普通字符串，不维护注册表、LRU 或异步执行框架。
真正用于 provider 前缀缓存的是最终请求中稳定的消息前缀，而不是本地字符串缓存。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence


async def memory_sections(memory_loader: Any, query: str = "") -> list[tuple[str, str]]:
    """分别加载长期 instructions 与本轮检索知识。

    两部分必须拆开：用户查询变化时，通常只有检索知识变化，不应因此把整份
    AGENT.md、CLAUDE.md 或 MEMORY.md 再写入一次 history。

    Memory 读取失败时抛出异常，由上层 dynamic section 走 error 语义（保留 baseline，
    不发送 removed），不得伪装成 section absent。
    """
    from .. import memory as _memory_pkg
    from ..memory.loader import MemoryBudgetError

    reset = getattr(memory_loader, "reset_cache", None)
    if callable(reset):
        # 记忆文件可能由工具在运行中修改，每轮构建前重新读取以保证立即生效。
        reset(reason="memory_sections_realtime_reload")

    try:
        files = await memory_loader.get_memory_files()
    except MemoryBudgetError:
        # Managed 装不下：向上抛，阻止本轮请求（不能静默半截 Managed）。
        raise
    except Exception as error:
        # 临时 IO/解析失败：向上抛，让 get_dynamic_context_sections 记为 error。
        raise RuntimeError(f"memory files load failed: {error}") from error

    report = None
    get_report = getattr(memory_loader, "get_last_budget_report", None)
    if callable(get_report):
        report = get_report()
    omitted = tuple(report.omitted) if report is not None else ()
    truncated_paths = (
        tuple(str(item[0].path) for item in report.truncated)
        if report is not None
        else ()
    )
    instructions = _memory_pkg.format_memory_files(
        files,
        omitted=omitted,
        truncated_paths=truncated_paths,
    ) if (files or omitted or truncated_paths) else ""

    knowledge = ""
    get_knowledge_context = getattr(memory_loader, "get_knowledge_context", None)
    if callable(get_knowledge_context):
        knowledge = await get_knowledge_context(query or "")

    sections: list[tuple[str, str]] = []
    if instructions.strip():
        sections.append(("instructions", instructions.strip()))
    if knowledge and knowledge.strip():
        sections.append((
            "knowledge",
            "# Retrieved knowledge\n" + knowledge.strip(),
        ))
    return sections


async def memory_section(memory_loader: Any, query: str = "") -> Optional[str]:
    """兼容旧调用点，返回合并后的记忆文本。"""
    sections = await memory_sections(memory_loader, query=query)
    if not sections:
        return None
    return "\n\n".join(text for _, text in sections)


def env_info_section(
    *,
    model: str,
    cwd: Optional[Path] = None,
    additional_directories: Optional[Sequence[Path]] = None,
) -> str:
    """生成当前 cwd、平台、shell 与模型信息。"""
    from ..prompts.env_info import compute_env_info

    return compute_env_info(
        model=model,
        cwd=cwd,
        additional_directories=additional_directories,
    )


def current_time_section() -> str:
    """返回稳定到“日期 + 时区”的时间上下文。

    精确到秒会让该块每轮都变化，既浪费上下文又迫使 history 持续追加无意义更新。
    模型需要精确当前时间时应调用系统工具，而不是依赖提示词快照。
    """
    local_now = datetime.now().astimezone()
    timezone_name = local_now.tzname() or "unknown"
    offset = local_now.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return (
        "# Current date\n"
        f"- Current local date: {local_now.date().isoformat()}\n"
        f"- Timezone: {timezone_name}\n"
        f"- UTC offset: {offset or 'unknown'}\n"
        "- Interpret relative dates from this date. Verify time-sensitive facts when needed."
    )


def language_section(language: Optional[str]) -> Optional[str]:
    """生成用户语言偏好；未配置时不注入。"""
    if not language or not language.strip():
        return None
    return (
        "# Language\n"
        f"The user's preferred working language is {language.strip()}. "
        "Default to it for prose responses."
    )


def mcp_instructions_section(mcp_clients: Optional[Sequence[Any]]) -> Optional[str]:
    """聚合当前已连接 MCP 服务公开的 instructions。"""
    if not mcp_clients:
        return None

    chunks: list[str] = []
    # 连接建立顺序可能受并发影响，按服务名排序可避免相同 MCP 集合产生不同指纹。
    sorted_clients = sorted(
        mcp_clients,
        key=lambda client: str(
            getattr(client, "name", None)
            or getattr(client, "server_name", None)
            or "<mcp>"
        ),
    )
    for client in sorted_clients:
        instructions = getattr(client, "instructions", None) or getattr(
            client, "server_instructions", None
        )
        name = getattr(client, "name", None) or getattr(client, "server_name", "<mcp>")
        if instructions and isinstance(instructions, str) and instructions.strip():
            chunks.append(f"## MCP server `{name}`\n{instructions.strip()}")
    if not chunks:
        return None
    return "# MCP server instructions\n\n" + "\n\n".join(chunks)


def session_guidance_section(
    *,
    enabled_tools: frozenset[str],
    skill_commands: Optional[Sequence[Any]] = None,
) -> str:
    """生成当前工具与技能集合的会话指导。"""
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
    if {"file_read", "file_write", "file_edit"}.intersection(enabled_tools):
        bits.append(
            "- Prefer `file_read` / `file_edit` / `file_write` for file I/O over invoking "
            "`cat` / `echo` through bash. Use `file_edit` for local replacements."
        )
    if skill_commands:
        names = ", ".join(sorted(getattr(s, "name", str(s)) for s in skill_commands))
        bits.append(
            f"- Skills available: {names}. Invoke a skill through the `skill` tool; "
            "the skill body is loaded lazily."
        )
    return "# Session guidance\n" + "\n".join(bits)


def token_budget_section(*, budget_directive: Optional[str] = None) -> Optional[str]:
    """生成用户显式传入的 token 预算约束。"""
    if not budget_directive or not budget_directive.strip():
        return None
    return (
        "# Token budget\n"
        f"User has set a token budget directive: `{budget_directive.strip()}`. "
        "Continue working until the budget is exhausted before declaring the task complete."
    )


__all__ = [
    "current_time_section",
    "env_info_section",
    "language_section",
    "mcp_instructions_section",
    "memory_section",
    "memory_sections",
    "session_guidance_section",
    "token_budget_section",
]
