"""SYSTEM_PROMPT_DYNAMIC_BOUNDARY —— 静态/动态切分点常量。

对应 claude-code 的同名常量。

它只是 list[str] 中的一个特殊字符串。get_system_prompt() 在拼装时把它
插在静态段(可全局缓存)与动态段(用户/会话特定)之间;
context.cache.split.split_sys_prompt_prefix() 用 list.index() 找到位置后
把列表切成两块,boundary 自身被丢弃,不会出现在最终发送给 LLM 的文本中。

单独成文件是为了避免 builder/split/blocks 之间的循环 import。
"""

from __future__ import annotations

from typing import Final


SYSTEM_PROMPT_DYNAMIC_BOUNDARY: Final[str] = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


__all__ = ["SYSTEM_PROMPT_DYNAMIC_BOUNDARY"]
