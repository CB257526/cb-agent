"""SystemPromptSectionCache + Section registry 单元测试。

覆盖:
- LRU 淘汰
- None 是合法缓存值
- _MISSING 哨兵
- 普通 Section 的 cache hit/miss
- DANGEROUS_uncached_* 每次重算
- resolve_system_prompt_sections 并发 + 过滤空段
- clear 后第二次 resolve 重新 compute
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from context.sections.cache import (
    SystemPromptSectionCache,
    _MISSING,
    clear_system_prompt_sections,
    get_system_prompt_section_cache,
)
from context.sections.registry import (
    DANGEROUS_uncached_system_prompt_section,
    resolve_system_prompt_sections,
    system_prompt_section,
)


def test_cache_set_get_basic():
    c = SystemPromptSectionCache(max_entries=10)
    assert c.get("missing") is _MISSING
    c.set("a", "value-a")
    assert c.get("a") == "value-a"
    assert c.has("a")


def test_cache_none_is_legal_value():
    c = SystemPromptSectionCache(max_entries=5)
    c.set("nullable", None)
    assert c.has("nullable")
    assert c.get("nullable") is None  # not _MISSING


def test_cache_lru_eviction():
    c = SystemPromptSectionCache(max_entries=3)
    c.set("a", "1"); c.set("b", "2"); c.set("c", "3")
    assert c.get("a") == "1"  # 刷新 a 到末端
    c.set("d", "4")  # 淘汰 b(最早未访问)
    assert c.get("b") is _MISSING
    assert c.get("a") == "1"
    assert c.get("c") == "3"
    assert c.get("d") == "4"


def test_cache_clear():
    c = SystemPromptSectionCache(max_entries=5)
    c.set("a", "1"); c.set("b", "2")
    assert len(c) == 2
    c.clear()
    assert len(c) == 0
    assert c.get("a") is _MISSING


def test_cache_set_overwrites_and_refreshes():
    c = SystemPromptSectionCache(max_entries=2)
    c.set("a", "1"); c.set("b", "2")
    c.set("a", "1-new")  # 覆盖 + 移到末端 -> b 变成最早
    c.set("c", "3")  # 应淘汰 b
    assert c.get("b") is _MISSING
    assert c.get("a") == "1-new"
    assert c.get("c") == "3"


def test_resolve_caches_simple_section():
    cache = SystemPromptSectionCache(max_entries=10)
    counter = {"n": 0}

    def compute():
        counter["n"] += 1
        return f"v{counter['n']}"

    sec = system_prompt_section("alpha", compute)
    out1 = asyncio.run(resolve_system_prompt_sections([sec], cache))
    out2 = asyncio.run(resolve_system_prompt_sections([sec], cache))
    assert out1 == ["v1"]
    assert out2 == ["v1"]  # 命中缓存,counter 不再加
    assert counter["n"] == 1


def test_resolve_filters_none_and_empty():
    cache = SystemPromptSectionCache(max_entries=10)
    secs = [
        system_prompt_section("a", lambda: "real"),
        system_prompt_section("b", lambda: None),
        system_prompt_section("c", lambda: "   "),  # 仅空白
        system_prompt_section("d", lambda: "another"),
    ]
    out = asyncio.run(resolve_system_prompt_sections(secs, cache))
    assert out == ["real", "another"]


def test_dangerous_uncached_recomputes_each_time():
    cache = SystemPromptSectionCache(max_entries=10)
    counter = {"n": 0}

    def compute():
        counter["n"] += 1
        return f"v{counter['n']}"

    sec = DANGEROUS_uncached_system_prompt_section(
        "volatile", compute, reason="MCP instructions change between turns"
    )
    out1 = asyncio.run(resolve_system_prompt_sections([sec], cache))
    out2 = asyncio.run(resolve_system_prompt_sections([sec], cache))
    assert out1 == ["v1"]
    assert out2 == ["v2"]
    # 缓存不应记录 cache_break section
    assert cache.get("volatile") is _MISSING


def test_dangerous_uncached_requires_reason():
    with pytest.raises(ValueError):
        DANGEROUS_uncached_system_prompt_section("x", lambda: "y", reason="")


def test_resolve_supports_async_compute():
    cache = SystemPromptSectionCache(max_entries=10)

    async def compute():
        await asyncio.sleep(0)
        return "from-async"

    sec = system_prompt_section("async-one", compute)
    out = asyncio.run(resolve_system_prompt_sections([sec], cache))
    assert out == ["from-async"]


def test_global_cache_singleton_clear():
    g = get_system_prompt_section_cache()
    g.set("x", "y")
    assert g.get("x") == "y"
    clear_system_prompt_sections()
    assert g.get("x") is _MISSING
