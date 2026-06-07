"""静态系统提示段 —— 无 I/O、无环境依赖的纯函数。

对应 claude-code/src/constants/prompts.ts 中的 simpleIntro / simpleSystem /
simpleDoingTasks / actions / usingYourTools / outputEfficiency 几段。

这些函数返回**确定性字符串**:相同入参永远产出相同输出,无需缓存即可放在
SYSTEM_PROMPT_DYNAMIC_BOUNDARY 之前共享 cache_scope=GLOBAL。

稳定文本集中放在 ``constant.system_prompt.ConstantSystemPrompt``。这里保留函数
是为了维持 prompts/builder.py 的分段语义，并给 output_style 这类少量包装逻辑
留入口。

不在这里:
- CLAUDE.md / env_info / mcp_instructions —— 它们是动态段(见 dynamic_sections.py)
- 当前启用工具列表 —— 工具集合会随 CLI/TUI/QQ、memory_system、MCP 变化,放在
  dynamic session_guidance 中，避免污染未来 provider prompt cache 的静态前缀
- 工具列表的具体 schema —— 那是 LLM API 的 tools 字段,不进 system prompt
"""

from __future__ import annotations

from typing import FrozenSet, Optional

from constant.system_prompt import ConstantSystemPrompt


def get_intro_section(output_style: Optional[str] = None) -> str:
    """身份声明段。

    output_style 在 cb-agent 里目前用不到(预留参数,对齐 claude-code 形态)。
    """
    base = ConstantSystemPrompt.INTRO_SECTION
    if output_style:
        return f"{base}\n\n[Output style override: {output_style}]"
    return base


def get_user_cosplay_section() -> str:
    """用户固定角色风格段。

    该段来自 ConstantSystemPrompt.USER_COSPLAY_PROMPT。它属于用户长期偏好，
    因此放在动态边界之前；如果用户经常改这个字段，provider prompt cache 自然
    会按新静态前缀重新命中，不影响当前时间/记忆等动态段的拆分。
    """

    return ConstantSystemPrompt.get_user_cosplay_section()


def get_system_section() -> str:
    """系统规则段 —— 工具结果处理、错误恢复、上下文规则。"""
    return ConstantSystemPrompt.SYSTEM_RULES_SECTION


def get_doing_tasks_section() -> str:
    """任务指导段 —— 何时直接动手,何时先澄清。"""
    return ConstantSystemPrompt.DOING_TASKS_SECTION


def get_actions_section() -> str:
    """行动指南段 —— 危险动作与可逆操作的判断。"""
    return ConstantSystemPrompt.ACTIONS_SECTION


def get_using_your_tools_section(enabled_tools: FrozenSet[str]) -> str:
    """工具使用规范段。

    当前启用工具名已经挪到 dynamic session_guidance 段。这里保留
    enabled_tools 参数是为了兼容旧调用点,但不把工具列表写进静态前缀。
    """

    del enabled_tools
    return ConstantSystemPrompt.TOOL_USAGE_RULES_SECTION


def get_local_agent_guidance_section() -> str:
    """cb-agent 本地固定行为偏好。"""

    return ConstantSystemPrompt.LOCAL_AGENT_GUIDANCE_SECTION


def get_output_efficiency_section() -> str:
    """输出效率约束段。"""
    return ConstantSystemPrompt.OUTPUT_EFFICIENCY_SECTION


__all__ = [
    "get_intro_section",
    "get_user_cosplay_section",
    "get_system_section",
    "get_doing_tasks_section",
    "get_actions_section",
    "get_using_your_tools_section",
    "get_local_agent_guidance_section",
    "get_output_efficiency_section",
]
