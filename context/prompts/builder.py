"""Chat Completions 提示词组装。

静态 system 始终位于请求开头；运行时上下文以具名块返回，由 AgentSession 比较内容
指纹并只追加变化块。该结构直接服务 provider 的前缀缓存，不再引入本地 Section
注册表或字符串 LRU。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from ..sections.dynamic_sections import (
    current_time_section,
    env_info_section,
    language_section,
    mcp_instructions_section,
    memory_sections,
    session_guidance_section,
    token_budget_section,
)
from ..sections.static_sections import (
    get_actions_section,
    get_doing_tasks_section,
    get_intro_section,
    get_local_agent_guidance_section,
    get_output_efficiency_section,
    get_system_section,
    get_using_your_tools_section,
    get_user_cosplay_section,
)


def get_static_system_prompt(
    *,
    enabled_tools: frozenset[str],
    output_style: Optional[str] = None,
) -> list[str]:
    """返回确定性的 system prompt 段列表。"""
    static_parts = [
        get_intro_section(output_style),
        get_user_cosplay_section(),
        get_system_section(),
        get_doing_tasks_section(),
        get_actions_section(),
        get_using_your_tools_section(enabled_tools),
        get_local_agent_guidance_section(),
        get_output_efficiency_section(),
    ]
    return [part for part in static_parts if part and part.strip()]


async def get_dynamic_context_sections(
    *,
    enabled_tools: frozenset[str],
    model: str,
    cwd: Optional[Path] = None,
    additional_directories: Optional[Sequence[Path]] = None,
    memory_loader: Any = None,
    mcp_clients: Optional[Sequence[Any]] = None,
    skill_commands: Optional[Sequence[Any]] = None,
    language: Optional[str] = None,
    budget_directive: Optional[str] = None,
    memory_query: str = "",
) -> list[tuple[str, str]]:
    """返回有序的运行时上下文块。

    名称是跨轮指纹键，文本是模型可见内容。顺序固定，确保相同状态生成相同请求。
    """
    sections: list[tuple[str, str]] = [
        (
            "session_guidance",
            session_guidance_section(
                enabled_tools=enabled_tools,
                skill_commands=skill_commands,
            ),
        ),
    ]
    if memory_loader is not None:
        sections.extend(await memory_sections(memory_loader, query=memory_query))
    sections.extend([
        ("current_date", current_time_section()),
        (
            "environment",
            env_info_section(
                model=model,
                cwd=cwd,
                additional_directories=additional_directories,
            ),
        ),
    ])

    optional_sections = [
        ("language", language_section(language)),
        ("mcp_instructions", mcp_instructions_section(mcp_clients)),
        ("token_budget", token_budget_section(budget_directive=budget_directive)),
    ]
    sections.extend((name, text) for name, text in optional_sections if text and text.strip())
    return [(name, text.strip()) for name, text in sections if text and text.strip()]


async def get_dynamic_context_prompt(**kwargs: Any) -> list[str]:
    """兼容入口：只返回动态块文本。"""
    return [text for _, text in await get_dynamic_context_sections(**kwargs)]


async def get_system_prompt(
    *,
    enabled_tools: frozenset[str],
    model: str,
    cwd: Optional[Path] = None,
    additional_directories: Optional[Sequence[Path]] = None,
    memory_loader: Any = None,
    mcp_clients: Optional[Sequence[Any]] = None,
    skill_commands: Optional[Sequence[Any]] = None,
    language: Optional[str] = None,
    output_style: Optional[str] = None,
    budget_directive: Optional[str] = None,
    memory_query: str = "",
) -> list[str]:
    """兼容入口：返回静态 system 段与动态上下文段。"""
    static = get_static_system_prompt(
        enabled_tools=enabled_tools,
        output_style=output_style,
    )
    dynamic = await get_dynamic_context_prompt(
        enabled_tools=enabled_tools,
        model=model,
        cwd=cwd,
        additional_directories=additional_directories,
        memory_loader=memory_loader,
        mcp_clients=mcp_clients,
        skill_commands=skill_commands,
        language=language,
        budget_directive=budget_directive,
        memory_query=memory_query,
    )
    return static + dynamic


__all__ = [
    "get_dynamic_context_prompt",
    "get_dynamic_context_sections",
    "get_static_system_prompt",
    "get_system_prompt",
]
