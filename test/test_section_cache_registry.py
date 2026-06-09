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
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import context.sections.dynamic_sections as dynamic_sections
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
    try:
        DANGEROUS_uncached_system_prompt_section("x", lambda: "y", reason="")
    except ValueError:
        return
    raise AssertionError("empty reason should raise ValueError")


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


class TestCurrentTimeSection(unittest.TestCase):
    def test_current_time_section_recomputes_each_time(self):
        """当前时间必须每轮重算，避免 system prompt 长时间停留在旧日期。"""
        from datetime import datetime as real_datetime
        from datetime import timezone as real_timezone

        class FakeDatetime(real_datetime):
            calls = 0

            @classmethod
            def now(cls, tz=None):
                cls.calls += 1
                value = real_datetime(2026, 6, 7, 12, 0, cls.calls, tzinfo=real_timezone.utc)
                if tz is None:
                    return value
                return value.astimezone(tz)

        with patch.object(dynamic_sections, "datetime", FakeDatetime):
            cache = SystemPromptSectionCache(max_entries=10)
            section = dynamic_sections.current_time_section()
            out1 = asyncio.run(resolve_system_prompt_sections([section], cache))
            out2 = asyncio.run(resolve_system_prompt_sections([section], cache))

        self.assertEqual(len(out1), 1)
        self.assertEqual(len(out2), 1)
        self.assertIn("# Current time", out1[0])
        self.assertIn("Current local date: 2026-06-07", out1[0])
        self.assertNotEqual(out1, out2)
        self.assertIs(cache.get("current_time"), _MISSING)


class TestMemorySectionRealtimeReload(unittest.TestCase):
    def test_memory_section_reloads_loader_each_resolve(self):
        """CLAUDE.md 记忆段必须每次 prompt 组装都重新读取。"""
        from context.memory.types import MemoryFileInfo

        class FakeMemoryLoader:
            def __init__(self) -> None:
                self.calls = 0
                self.reset_reasons: list[str] = []

            def reset_cache(self, reason: str = "") -> None:
                self.reset_reasons.append(reason)

            async def get_memory_files(self):
                self.calls += 1
                return [
                    MemoryFileInfo(
                        path=Path(f"CLAUDE-{self.calls}.md").resolve(strict=False),
                        type="Project",
                        content=f"memory-v{self.calls}",
                    )
                ]

        loader = FakeMemoryLoader()
        cache = SystemPromptSectionCache(max_entries=10)
        section = dynamic_sections.memory_section(loader)

        out1 = asyncio.run(resolve_system_prompt_sections([section], cache))
        out2 = asyncio.run(resolve_system_prompt_sections([section], cache))

        self.assertEqual(loader.calls, 2)
        self.assertEqual(
            loader.reset_reasons,
            ["memory_section_realtime_reload", "memory_section_realtime_reload"],
        )
        self.assertIn("memory-v1", out1[0])
        self.assertIn("memory-v2", out2[0])
        self.assertNotEqual(out1, out2)
        self.assertIs(cache.get("memory"), _MISSING)

    def test_memory_section_reads_updated_claude_md_without_restart(self):
        """真实 CLAUDE.md 在运行中修改后,下一次 prompt 组装应看到新内容。"""
        from context.memory.loader import MemoryLoader

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            claude_md = root / "CLAUDE.md"
            claude_md.write_text("memory-from-v1", encoding="utf-8")
            loader = MemoryLoader(
                cwd=root,
                include_managed=False,
                include_user=False,
            )
            cache = SystemPromptSectionCache(max_entries=10)
            section = dynamic_sections.memory_section(loader)

            out1 = asyncio.run(resolve_system_prompt_sections([section], cache))
            claude_md.write_text("memory-from-v2", encoding="utf-8")
            out2 = asyncio.run(resolve_system_prompt_sections([section], cache))

        self.assertIn("memory-from-v1", out1[0])
        self.assertIn("memory-from-v2", out2[0])
        self.assertNotIn("memory-from-v1", out2[0])
        self.assertIs(cache.get("memory"), _MISSING)
