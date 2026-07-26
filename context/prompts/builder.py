"""Chat Completions 提示词组装。

静态 system 始终位于请求开头；运行时上下文以具名块返回，由 AgentSession 比较内容
指纹并只追加变化块。该结构直接服务 provider 的前缀缓存，不再引入本地 Section
注册表或字符串 LRU。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal, Optional, Sequence

from ..world_state import DynamicSectionResult

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
) -> list[DynamicSectionResult]:
    """返回有序且带三态、持久化语义的运行时上下文块。

    每个可选持久块都必须明确返回 ``absent``，读取异常则返回 ``error``。
    Session 因此不需要根据 section 名猜测持久化方式，也不会把异常误判为删除。
    """

    def _sync_section(
        name: str,
        loader: Callable[[], Optional[str]],
        *,
        persistence: Literal["persistent", "request_only"] = "persistent",
    ) -> DynamicSectionResult:
        try:
            text = loader()
        except Exception as error:  # noqa: BLE001
            return DynamicSectionResult.error_result(
                name,
                str(error),
                persistence=persistence,
            )
        if text and str(text).strip():
            return DynamicSectionResult.present(
                name,
                str(text),
                persistence=persistence,
            )
        return DynamicSectionResult.absent(
            name,
            persistence=persistence,
        )

    sections: list[DynamicSectionResult] = [
        _sync_section(
            "session_guidance",
            lambda: session_guidance_section(
                enabled_tools=enabled_tools,
                skill_commands=skill_commands,
            ),
        ),
    ]
    if memory_loader is not None:
        try:
            memory_values = await memory_sections(memory_loader, query=memory_query)
        except Exception as error:  # noqa: BLE001
            # instructions 是关键持久块；knowledge 只影响本次检索，失败时不污染 baseline。
            sections.extend([
                DynamicSectionResult.error_result("instructions", str(error)),
                DynamicSectionResult.error_result(
                    "knowledge",
                    str(error),
                    persistence="request_only",
                ),
            ])
        else:
            memory_map = {name: text for name, text in memory_values}
            instructions = str(memory_map.get("instructions") or "").strip()
            knowledge = str(memory_map.get("knowledge") or "").strip()
            sections.append(
                DynamicSectionResult.present("instructions", instructions)
                if instructions
                else DynamicSectionResult.absent("instructions")
            )
            sections.append(
                DynamicSectionResult.present(
                    "knowledge",
                    knowledge,
                    persistence="request_only",
                )
                if knowledge
                else DynamicSectionResult.absent(
                    "knowledge",
                    persistence="request_only",
                )
            )
    sections.extend([
        _sync_section("current_date", current_time_section),
        _sync_section(
            "environment",
            lambda: env_info_section(
                model=model,
                cwd=cwd,
                additional_directories=additional_directories,
            ),
        ),
    ])

    sections.extend([
        _sync_section("language", lambda: language_section(language)),
        _sync_section("mcp_instructions", lambda: mcp_instructions_section(mcp_clients)),
        _sync_section(
            "token_budget",
            lambda: token_budget_section(budget_directive=budget_directive),
        ),
    ])
    return sections


async def get_dynamic_context_prompt(**kwargs: Any) -> list[str]:
    """兼容入口：只返回动态块文本。"""
    return [
        section.text
        for section in await get_dynamic_context_sections(**kwargs)
        if section.status == "present" and section.text
    ]


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
