"""Section 子模块。

对应 claude-code/src/constants/systemPromptSections.ts + 静态/动态段实现。
"""

from .cache import (
    SystemPromptSectionCache,
    clear_system_prompt_sections,
    get_system_prompt_section_cache,
)
from .registry import (
    DANGEROUS_uncached_system_prompt_section,
    SystemPromptSection,
    resolve_system_prompt_sections,
    system_prompt_section,
)

__all__ = [
    "DANGEROUS_uncached_system_prompt_section",
    "SystemPromptSection",
    "SystemPromptSectionCache",
    "clear_system_prompt_sections",
    "get_system_prompt_section_cache",
    "resolve_system_prompt_sections",
    "system_prompt_section",
]
