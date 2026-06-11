"""Explicit knowledge-base tools for lightweight and full memory modes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List

from context.memory.knowledge import KnowledgeBase
from context.memory.paths import get_knowledge_root
from memory.feature_flags import is_full_memory_enabled
from tools.tool import Tool, ToolParameter


MAX_WRITE_BODY_CHARS = 20_000
MAX_SEARCH_CHARS = 10_000


def _knowledge_base() -> KnowledgeBase:
    root = get_knowledge_root(Path.cwd())
    namespace_digest = hashlib.sha1(
        str(root).lower().encode("utf-8", errors="ignore")
    ).hexdigest()[:12]
    return KnowledgeBase(root, namespace="tool:" + namespace_digest)


def _backend_label(kb: KnowledgeBase) -> str:
    return "markdown+rag" if kb.enable_rag else "markdown"


def _coerce_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value).replace("，", ",").split(",")
    tags: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = str(item).strip()
        if not tag or tag in seen:
            continue
        tags.append(tag)
        seen.add(tag)
    return tags[:12]


def _coerce_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


class KnowledgeWriteTool(Tool):
    """Create or update a structured Markdown knowledge page."""

    def __init__(self) -> None:
        super().__init__(
            name="knowledge_write",
            description=(
                "写入结构化知识库。适合在对话中识别到可复用的项目知识、架构决策、"
                "接口约定、工作流程、用户确认的长期事实时调用。默认写入 Markdown "
                "知识页并刷新 index/graph；当 CBAGENT_ENABLE_FULL_MEMORY=1 时，"
                "会额外尝试写入/更新 RAG 向量索引。不要写临时猜测或未经确认的信息。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="title",
                type="string",
                description="知识页标题，应该简短、稳定、可复用。",
                required=True,
            ),
            ToolParameter(
                name="body",
                type="string",
                description=(
                    "Markdown 正文。应整理为结构化知识，而不是完整粘贴聊天记录。"
                    "可以使用 [[页面标题]] 建立交叉引用。"
                ),
                required=True,
            ),
            ToolParameter(
                name="tags",
                type="array",
                description="标签数组，例如 memory、architecture、api、项目名等。",
                required=False,
                items={"type": "string"},
            ),
            ToolParameter(
                name="source",
                type="string",
                description="知识来源说明，例如 conversation、user-confirmed、tool-result。",
                required=False,
                default="tool",
            ),
            ToolParameter(
                name="metadata_json",
                type="string",
                description="可选 JSON 对象字符串，会合并进知识页 frontmatter。",
                required=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        title = parameters.get("title")
        body = parameters.get("body")
        return isinstance(title, str) and bool(title.strip()) and isinstance(body, str) and bool(body.strip())

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps(
                {"ok": False, "error": "需要 title(str) 和 body(str)。"},
                ensure_ascii=False,
            )

        title = str(parameters["title"]).strip()
        body = str(parameters["body"]).strip()
        if len(body) > MAX_WRITE_BODY_CHARS:
            body = body[:MAX_WRITE_BODY_CHARS].rstrip() + "\n\n...[truncated]"

        metadata: dict[str, Any] = {
            "capture": "tool",
            "full_memory_enabled": is_full_memory_enabled(),
        }
        metadata_json = str(parameters.get("metadata_json") or "").strip()
        if metadata_json:
            try:
                parsed = json.loads(metadata_json)
            except json.JSONDecodeError as exc:
                return json.dumps(
                    {"ok": False, "error": f"metadata_json 不是合法 JSON: {exc}"},
                    ensure_ascii=False,
                )
            if not isinstance(parsed, dict):
                return json.dumps(
                    {"ok": False, "error": "metadata_json 必须是 JSON 对象。"},
                    ensure_ascii=False,
                )
            metadata.update(parsed)

        try:
            kb = _knowledge_base()
            path = kb.upsert_page(
                title=title,
                body=body,
                tags=_coerce_tags(parameters.get("tags")),
                source=str(parameters.get("source") or "tool").strip() or "tool",
                metadata=metadata,
            )
            index = kb.read_index()
            return json.dumps(
                {
                    "ok": True,
                    "backend": _backend_label(kb),
                    "knowledge_root": str(kb.root),
                    "path": str(path),
                    "pages": len(index.get("pages") or []),
                    "message": "知识已写入并刷新索引。",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"ok": False, "error": f"知识写入失败: {exc}"},
                ensure_ascii=False,
            )


class KnowledgeSearchTool(Tool):
    """Search the structured knowledge base."""

    def __init__(self) -> None:
        super().__init__(
            name="knowledge_search",
            description=(
                "检索结构化知识库。默认从 Markdown 知识页和 index 中做关键词检索；"
                "当 CBAGENT_ENABLE_FULL_MEMORY=1 时，会额外尝试 RAG/向量检索。"
                "适合在回答依赖项目约定、历史架构、接口规则、长期知识的问题前调用。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="检索问题或关键词。",
                required=True,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description="最多返回的知识片段数。",
                required=False,
                default=5,
            ),
            ToolParameter(
                name="max_chars",
                type="integer",
                description="返回内容最大字符数。",
                required=False,
                default=3500,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        query = parameters.get("query")
        return isinstance(query, str) and bool(query.strip())

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps(
                {"ok": False, "error": "需要 query(str)。"},
                ensure_ascii=False,
            )
        query = str(parameters["query"]).strip()
        limit = _coerce_int(parameters.get("limit"), default=5, low=1, high=10)
        max_chars = _coerce_int(parameters.get("max_chars"), default=3500, low=200, high=MAX_SEARCH_CHARS)

        try:
            kb = _knowledge_base()
            text = kb.render_related_context(query, limit=limit, max_chars=max_chars)
            return json.dumps(
                {
                    "ok": True,
                    "backend": _backend_label(kb),
                    "knowledge_root": str(kb.root),
                    "query": query,
                    "chars": len(text or ""),
                    "result": text,
                    "message": "未找到相关知识。" if not text else "已返回相关知识。",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {"ok": False, "error": f"知识检索失败: {exc}"},
                ensure_ascii=False,
            )


__all__ = ["KnowledgeSearchTool", "KnowledgeWriteTool"]
