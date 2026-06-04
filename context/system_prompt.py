"""build_effective_system_prompt —— system prompt 优先级链。

对应 claude-code/src/utils/systemPrompt.ts:buildEffectiveSystemPrompt。

简化实现: cb-agent 当前不区分 user-level system prompt override 与
output_style 的多层叠加,只把 base + appended 拼接。预留接口以便后续
接入 settings.system_prompt_append。
"""

from __future__ import annotations

from typing import Optional, Sequence


def build_effective_system_prompt(
    *,
    base: Sequence[str],
    appended_user_system: Optional[str] = None,
) -> list[str]:
    """把基础 system prompt 与用户追加段拼起来。

    appended_user_system 来自 settings.system_prompt_append,允许用户在
    项目级或全局级追加自定义指令(优先级介于 CLAUDE.md 与运行时段之间)。
    """
    out = [s for s in base if s and s.strip()]
    if appended_user_system and appended_user_system.strip():
        out.append(appended_user_system.strip())
    return out


__all__ = ["build_effective_system_prompt"]
