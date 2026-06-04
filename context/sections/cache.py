"""SystemPromptSectionCache —— Section 级 LRU 缓存。

对应 claude-code 的 STATE.systemPromptSectionCache（Map<string, string | null>）。

设计要点：
- OrderedDict 实现 LRU，max_entries=100；超量时 popitem(last=False) 淘汰最早。
- _MISSING 哨兵区分"未缓存"与"缓存了 None"。None 是合法值（表示已确认这段
  不会出现，下一轮跳过 compute）。
- threading.Lock 保护 get/set/clear 的短临界区。Python GIL 下短临界区不会
  与上下游事件循环冲突。
- 模块单例 _GLOBAL_CACHE：Section 名直接当 cache key，进程内全局共享。
- /clear 与 /compact 时调用 clear_system_prompt_sections() 让缓存失效。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Optional


_MISSING = object()


class SystemPromptSectionCache:
    """Section 名 -> 计算结果的 LRU。"""

    def __init__(self, max_entries: int = 100) -> None:
        self._cache: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._cache

    def get(self, name: str):
        """返回缓存值或 _MISSING。命中时同时刷新到 LRU 末端。"""
        with self._lock:
            if name not in self._cache:
                return _MISSING
            self._cache.move_to_end(name)
            return self._cache[name]

    def set(self, name: str, value: Optional[str]) -> None:
        with self._lock:
            if name in self._cache:
                self._cache.move_to_end(name)
            self._cache[name] = value
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)


_GLOBAL_CACHE = SystemPromptSectionCache()


def get_system_prompt_section_cache() -> SystemPromptSectionCache:
    """返回模块单例缓存。Section 注册表与 resolve 流程共用同一份。"""
    return _GLOBAL_CACHE


def clear_system_prompt_sections() -> None:
    """清空全局 Section 缓存。

    /clear 与 /compact 命令、MCP connect/disconnect、settings 热更新都应触发。
    """
    _GLOBAL_CACHE.clear()


__all__ = [
    "SystemPromptSectionCache",
    "get_system_prompt_section_cache",
    "clear_system_prompt_sections",
    "_MISSING",
]
