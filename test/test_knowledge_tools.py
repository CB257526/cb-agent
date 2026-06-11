from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.platforms.permissions import sensitive_tool_reason
from tools.tools.knowledge_tool import KnowledgeSearchTool, KnowledgeWriteTool


def test_knowledge_write_and_search_use_markdown_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CBAGENT_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    monkeypatch.delenv("CBAGENT_ENABLE_FULL_MEMORY", raising=False)

    write_result = json.loads(
        KnowledgeWriteTool().run(
            {
                "title": "Graph API Authentication",
                "body": "Graph API calls must use project scoped tokens and rotate them monthly.",
                "tags": ["api", "security"],
                "source": "test",
            }
        )
    )

    assert write_result["ok"]
    assert write_result["backend"] == "markdown"
    assert Path(write_result["path"]).is_file()
    assert (tmp_path / "knowledge" / "index.json").is_file()
    assert (tmp_path / "knowledge" / "graph.json").is_file()

    search_result = json.loads(
        KnowledgeSearchTool().run(
            {
                "query": "How should Graph API authentication work?",
                "limit": 3,
            }
        )
    )

    assert search_result["ok"]
    assert search_result["backend"] == "markdown"
    assert "Graph API Authentication" in search_result["result"]
    assert "project scoped tokens" in search_result["result"]


def test_knowledge_write_rejects_invalid_metadata_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CBAGENT_KNOWLEDGE_DIR", str(tmp_path / "knowledge"))

    result = json.loads(
        KnowledgeWriteTool().run(
            {
                "title": "Invalid Metadata",
                "body": "Body",
                "metadata_json": "[1, 2, 3]",
            }
        )
    )

    assert not result["ok"]
    assert "metadata_json" in result["error"]


def test_knowledge_tools_platform_sensitivity():
    assert sensitive_tool_reason("knowledge_search", {"query": "memory"}) == ""
    assert "修改结构化知识库" in sensitive_tool_reason(
        "knowledge_write",
        {"title": "Memory", "body": "Persistent fact"},
    )
