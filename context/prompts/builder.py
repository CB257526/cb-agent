"""get_system_prompt —— system prompt 主组装入口。

对应 claude-code/src/constants/prompts.ts:getSystemPrompt。

组装顺序(严格对齐):
    [intro, system, doing_tasks, actions, using_tools, output_efficiency,
     SYSTEM_PROMPT_DYNAMIC_BOUNDARY (条件),
     session_guidance, memory, env_info, language,
     mcp_instructions, token_budget]

返回 list[str](不预先 join 成单 string),下游 cache split 需要分段视图。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from ..cache.scope import should_use_global_cache_scope
from ..sections.cache import get_system_prompt_section_cache
from ..sections.dynamic_sections import (
    env_info_section,
    language_section,
    mcp_instructions_section,
    memory_section,
    session_guidance_section,
    token_budget_section,
)
from ..sections.registry import resolve_system_prompt_sections
from ..sections.static_sections import (
    get_actions_section,
    get_doing_tasks_section,
    get_intro_section,
    get_output_efficiency_section,
    get_system_section,
    get_using_your_tools_section,
)
from .boundary import SYSTEM_PROMPT_DYNAMIC_BOUNDARY


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
) -> list[str]:
    """组装当前会话的 system prompt(已 resolve、过滤空段)。

    入参意图:
        enabled_tools: registry 当前启用的工具名集合。
        memory_loader: MemoryLoader 实例;为 None 时 memory section 跳过。
        mcp_clients:   连接中的 MCP 客户端列表(每个有 .instructions / .name)。
        skill_commands: SkillManager.list_commands() 返回的 skill 列表。

    返回 list[str],其中可能含 SYSTEM_PROMPT_DYNAMIC_BOUNDARY 占位符,
    交给 build_system_prompt_blocks() 切分后再发给 LLM。
    """
    static_parts: list[str] = [
        get_intro_section(output_style),
        get_system_section(),
        get_doing_tasks_section(),
        get_actions_section(),
        get_using_your_tools_section(enabled_tools),
        get_output_efficiency_section(),
    ]

    dynamic_section_objs = []
    sg = session_guidance_section(
        enabled_tools=enabled_tools, skill_commands=skill_commands
    )
    if sg is not None:
        dynamic_section_objs.append(sg)
    if memory_loader is not None:
        dynamic_section_objs.append(memory_section(memory_loader))
    dynamic_section_objs.append(
        env_info_section(
            model=model,
            cwd=cwd,
            additional_directories=additional_directories,
        )
    )
    lang = language_section(language)
    if lang is not None:
        dynamic_section_objs.append(lang)
    mcp = mcp_instructions_section(mcp_clients)
    if mcp is not None:
        dynamic_section_objs.append(mcp)
    budget = token_budget_section(budget_directive=budget_directive)
    if budget is not None:
        dynamic_section_objs.append(budget)

    resolved_dynamic = await resolve_system_prompt_sections(
        dynamic_section_objs,
        get_system_prompt_section_cache(),
    )

    out: list[str] = [s for s in static_parts if s and s.strip()]
    if should_use_global_cache_scope(model):
        out.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)
    out.extend(resolved_dynamic)
    return out


__all__ = ["get_system_prompt"]
