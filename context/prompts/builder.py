"""Chat Completions prompt 组装 —— 静态 system + 动态上下文分离。

**核心设计: 稳定前缀 = 缓存命中**

主流 LLM provider(DeepSeek / OpenAI / Anthropic)都支持某种形式的 prompt cache,
其共同机制是: 对 messages 数组做前缀匹配,匹配到的最长不变前缀可以复用
已缓存的 KV cache,只对新增部分做 prefill。

System message 是 messages[0],位于最前。如果 system message 里包含每轮变化
的内容(当前时间、env_info、CLAUDE.md、MCP 指令、token 预算等),整个前缀
就变了 → 缓存永远不命中。

**拆分方案:**

┌─────────────────────────────────────────────────────────┐
│ get_static_system_prompt() → messages[0].role=system    │
│ 纯确定性内容(intro/行为规则/工具使用/output 格式)       │
│ 相同 (model, enabled_tools, output_style) 下永远不变    │
│ ← provider 端跨轮缓存的稳定前缀                          │
├─────────────────────────────────────────────────────────┤
│ get_dynamic_context_prompt() → messages[N].role=user    │
│ <context-update> 包装的运行时上下文:                     │
│ CLAUDE.md memory / env_info / MCP / 技能 / 时间 / 预算  │
│ ← 每轮可能变化,作为低优先级 user 消息注入                │
└─────────────────────────────────────────────────────────┘

get_system_prompt() 是向后兼容的合并入口,新代码应直接调用
get_static_system_prompt() 和 get_dynamic_context_prompt() 分别处理。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from ..sections.cache import get_system_prompt_section_cache
from ..sections.dynamic_sections import (
    current_time_section,
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
    """返回确定性 system prompt 段列表,用于 Chat Completions 首条 system message。

    这些段的特点:
    - 纯确定性: 相同入参 → 相同输出,不含时间/env_info/CLAUDE.md 等变动数据
    - 稳定的: 文本来自 constant.system_prompt.ConstantSystemPrompt,只在发版时更新
    - 可缓存: 作为 messages[0] 时,provider 端可以跨轮复用 KV cache

    包含的段:
    1. intro — "You are an interactive agent..."
    2. user_cosplay — 环境/身份/限制说明
    3. system — 系统运行规则
    4. doing_tasks — 任务执行原则
    5. actions — 可用行动分类
    6. using_your_tools — 工具使用指南(含 enabled_tools 列表)
    7. local_agent_guidance — 子代理使用指导
    8. output_efficiency — 输出效率规则
    """
    static_parts: list[str] = [
        get_intro_section(output_style),
        get_user_cosplay_section(),
        get_system_section(),
        get_doing_tasks_section(),
        get_actions_section(),
        get_using_your_tools_section(enabled_tools),
        get_local_agent_guidance_section(),
        get_output_efficiency_section(),
    ]
    return [s for s in static_parts if s and s.strip()]


async def get_dynamic_context_prompt(
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
) -> list[str]:
    """异步解析运行时上下文段,用于低优先级 context-update user 消息。

    与 get_static_system_prompt 不同,这些段的值取决于运行环境:
    - session_guidance:  会话级指导(含当前可用技能列表,每轮可能变化)
    - memory_section:    多级 CLAUDE.md 加载(用户编辑后下次即生效)
    - current_time:      当前时间(每轮变化 → 不能进静态 system)
    - env_info:          cwd/platform/shell 等环境信息(可能随 /cd 变化)
    - language_section:  用户语言偏好
    - mcp_instructions:  已连接的 MCP 服务器指令(服务器上线/离线会变)
    - token_budget:      上下文窗口预算提示(依赖 model → context window mapping)

    这些段的解析走 SystemPromptSection LRU 缓存:
    - env_info 按 (model, platform, cwd, shell) 缓存,切换目录后重算一次
    - CLAUDE.md memory 段按 (cwd, memory_query) 缓存
    - 缓存节省了字符串常量重复渲染,但缓存键本身每轮都可能不同

    所有段通过 resolve_system_prompt_sections() 统一解析(含缓存查询/过期判断),
    最终返回过滤掉空字符串的 list[str]。
    """
    dynamic_section_objs = []
    # 会话指导(含技能命令列表) —— 技能加载/卸载后可能变化
    sg = session_guidance_section(
        enabled_tools=enabled_tools, skill_commands=skill_commands
    )
    if sg is not None:
        dynamic_section_objs.append(sg)
    # 多级 CLAUDE.md memory —— memory_loader 为 None 时跳过(--bare 模式)
    if memory_loader is not None:
        dynamic_section_objs.append(memory_section(memory_loader, query=memory_query))
    # 当前时间 —— 每轮变化,必须动态
    dynamic_section_objs.append(current_time_section())
    # 环境信息(cwd / platform / shell)
    dynamic_section_objs.append(
        env_info_section(
            model=model,
            cwd=cwd,
            additional_directories=additional_directories,
        )
    )
    # 语言偏好
    lang = language_section(language)
    if lang is not None:
        dynamic_section_objs.append(lang)
    # MCP 指令
    mcp = mcp_instructions_section(mcp_clients)
    if mcp is not None:
        dynamic_section_objs.append(mcp)
    # Token 预算指令
    budget = token_budget_section(budget_directive=budget_directive)
    if budget is not None:
        dynamic_section_objs.append(budget)

    resolved_dynamic = await resolve_system_prompt_sections(
        dynamic_section_objs,
        get_system_prompt_section_cache(),
    )
    return resolved_dynamic


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
    """向后兼容的合并入口: 返回 static + dynamic 全部段。

    新代码应分别调用:
    - ``get_static_system_prompt()`` → 放 messages[0].role=system(稳定可缓存)
    - ``get_dynamic_context_prompt()`` → 放 context-update user 消息(变动部分)

    本函数仅供尚未迁移到分离模式的旧调用点使用。它把静态和动态段合并为
    单个 list[str],下游需要自己决定如何拆分成 API messages。
    """
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
    "get_static_system_prompt",
    "get_system_prompt",
]
