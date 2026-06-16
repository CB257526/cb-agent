from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from memory.embedding import refresh_embedder
from context.memory.knowledge import KnowledgeBase
from context.memory.loader import MemoryLoader
from context.sections.dynamic_sections import memory_section
from context.sections.cache import SystemPromptSectionCache
from context.sections.registry import resolve_system_prompt_sections


def test_memory_loader_loads_global_project_and_short_term_layers(
    tmp_path: Path,
    monkeypatch,
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CBAGENT_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    global_dir = home / ".cbagent"
    global_dir.mkdir(parents=True)
    (global_dir / "AGENT.md").write_text("global-agent", encoding="utf-8")
    (global_dir / "USER.md").write_text("global-user", encoding="utf-8")
    (global_dir / "RULE.md").write_text("global-rule", encoding="utf-8")
    (global_dir / "MEMORY.md").write_text("global-memory", encoding="utf-8")
    (home / "MEMORY.md").write_text("workspace-root-memory", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    (project / "AGENT.md").write_text("project-agent", encoding="utf-8")
    (project / ".cbagent").mkdir()
    (project / ".cbagent" / "SHORT_TERM.md").write_text(
        "short-term-now",
        encoding="utf-8",
    )

    loader = MemoryLoader(
        cwd=project,
        include_managed=False,
        include_user=True,
        include_knowledge=False,
    )
    files = asyncio.run(loader.get_memory_files())

    typed = [(f.type, f.content.strip()) for f in files]
    assert ("Global", "global-agent") in typed
    assert ("Global", "global-user") in typed
    assert ("Global", "global-rule") in typed
    assert ("Global", "global-memory") in typed
    assert ("Global", "workspace-root-memory") in typed
    assert ("Project", "project-agent") in typed
    assert ("ShortTerm", "short-term-now") in typed

    type_order = [f.type for f in files]
    assert max(i for i, t in enumerate(type_order) if t == "Global") < type_order.index("Project")
    assert type_order.index("Project") < type_order.index("ShortTerm")
    contents = [f.content.strip() for f in files]
    assert contents.index("global-memory") < contents.index("workspace-root-memory")


def test_knowledge_base_pages_index_and_graph(tmp_path: Path):
    kb = KnowledgeBase(tmp_path / "knowledge", enable_rag=False)
    first = kb.upsert_page(
        title="Memory Architecture",
        body="Layered memory uses Global, Project, and ShortTerm context.",
        tags=["memory"],
        source="test",
    )
    second = kb.upsert_page(
        title="RAG Context",
        body="RAG Context should refer to Memory Architecture when retrieving pages.",
        tags=["rag"],
        source="test",
    )

    assert first.is_file()
    assert second.is_file()
    index = json.loads((tmp_path / "knowledge" / "index.json").read_text(encoding="utf-8"))
    graph = json.loads((tmp_path / "knowledge" / "graph.json").read_text(encoding="utf-8"))

    assert len(index["pages"]) == 2
    assert {p["title"] for p in index["pages"]} == {"Memory Architecture", "RAG Context"}
    assert any(edge["type"] == "references" for edge in graph["edges"])
    assert kb.read_index()["pages"]
    assert kb.read_graph()["nodes"]

    context = kb.render_related_context("How does Memory Architecture work?", max_chars=1200)
    assert "Memory Architecture" in context
    assert "Layered memory" in context


def test_knowledge_rag_is_opt_in(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CBAGENT_ENABLE_FULL_MEMORY", raising=False)
    assert not KnowledgeBase(tmp_path / "default").enable_rag

    monkeypatch.setenv("CBAGENT_ENABLE_FULL_MEMORY", "1")
    assert KnowledgeBase(tmp_path / "enabled").enable_rag


def test_embedding_model_is_full_memory_opt_in(monkeypatch):
    monkeypatch.delenv("CBAGENT_ENABLE_FULL_MEMORY", raising=False)

    with pytest.raises(RuntimeError, match="Full memory is disabled"):
        refresh_embedder()


def test_capture_turn_updates_memory_and_knowledge(tmp_path: Path):
    kb = KnowledgeBase(tmp_path / "knowledge", enable_rag=False)
    memory_path = tmp_path / "MEMORY.md"

    result = kb.capture_turn(
        user_text="请记住我的偏好：以后解释架构时先给三层结构。",
        assistant_text="已记录。这个记忆架构设计包含全局、项目和短期三层。",
        long_term_memory_path=memory_path,
    )

    # capture_turn 现在只负责 MEMORY.md 长期记忆(显式"请记住"类触发)。
    # 结构化知识页自动捕获已移除，改由模型显式调用 knowledge_write 工具，
    # 因此这里不应再产生 pages。
    assert result.memory_updated
    assert not result.pages
    assert "三层结构" in memory_path.read_text(encoding="utf-8")


def test_memory_section_includes_structured_knowledge_context(tmp_path: Path):
    class FakeMemoryLoader:
        def __init__(self) -> None:
            self.reset_reasons: list[str] = []

        def reset_cache(self, reason: str = "") -> None:
            self.reset_reasons.append(reason)

        async def get_memory_files(self):
            return []

        async def get_knowledge_context(self, query: str, **kwargs):
            assert query == "memory architecture"
            return "Knowledge hit: layered memory context"

    loader = FakeMemoryLoader()
    section = memory_section(loader, query="memory architecture")
    out = asyncio.run(resolve_system_prompt_sections([section], SystemPromptSectionCache()))

    assert "Knowledge hit: layered memory context" in out[0]
    assert loader.reset_reasons == ["memory_section_realtime_reload"]
