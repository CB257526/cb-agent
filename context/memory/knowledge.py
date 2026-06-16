"""Workspace knowledge base support for cb-agent memory.

The knowledge base is intentionally file-first:

```
~/knowledge/
  README.md
  index.json
  graph.json
  pages/
    some-topic.md
```

Markdown pages are the lightweight source of truth. ``index.json`` and
``graph.json`` are stable read interfaces for a future web UI or graph viewer.
When the vector/RAG stack is available, pages are also indexed there as a
best-effort full-memory backend.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from memory.feature_flags import is_full_memory_enabled

from .paths import get_knowledge_root


logger = logging.getLogger(__name__)


MAX_AUTO_PAGE_CHARS = 5000
MEMORY_TRIGGERS = (
    "remember",
    "note that",
    "my preference",
    "i prefer",
    "以后",
    "记住",
    "记下来",
    "偏好",
    "我的",
    "重要",
    "必须",
    "不要忘",
)


@dataclass
class KnowledgeCaptureResult:
    """Result returned after automatic memory/knowledge capture."""

    memory_updated: bool = False
    pages: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clip_text(text: Any, limit: int) -> str:
    value = "" if text is None else str(text)
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _word_terms(text: str) -> set[str]:
    terms = set(re.findall(r"[A-Za-z0-9_\-]{2,}", text.lower()))
    for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.add(token)
    return terms


def _stable_slug(title: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    digest = hashlib.sha1(title.encode("utf-8", errors="ignore")).hexdigest()[:8]
    if ascii_slug:
        return f"{ascii_slug[:60].strip('-')}-{digest}"
    return f"page-{digest}"


def _json_default(obj: Any) -> str:
    return str(obj)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=_json_default) + "\n",
        encoding="utf-8",
    )


def _frontmatter_value(value: Any) -> str:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value).replace("\n", " ")


def _format_frontmatter(meta: dict[str, Any]) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {_frontmatter_value(value)}")
    lines.append("---")
    return "\n".join(lines)


def _parse_simple_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    raw = text[4:end].strip()
    body = text[end + len("\n---") :].lstrip("\n")
    meta: dict[str, Any] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value.startswith("[") or value.startswith("{"):
            try:
                meta[key] = json.loads(value)
                continue
            except Exception:
                pass
        meta[key] = value
    return meta, body


class KnowledgeBase:
    """Markdown knowledge base with optional vector/RAG indexing."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        namespace: str = "global",
        enable_rag: Optional[bool] = None,
    ) -> None:
        self.root = (root or get_knowledge_root()).expanduser().resolve()
        self.pages_dir = self.root / "pages"
        self.index_path = self.root / "index.json"
        self.graph_path = self.root / "graph.json"
        self.namespace = namespace
        if enable_rag is None:
            enable_rag = is_full_memory_enabled()
        self.enable_rag = enable_rag
        self._rag_pipeline: Optional[dict[str, Any]] = None
        self._rag_failed = False

    def ensure_structure(self) -> None:
        self.pages_dir.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(
                "# cb-agent Knowledge Base\n\n"
                "This directory is maintained by cb-agent.\n\n"
                "## Layout\n\n"
                "- `pages/` contains structured Markdown knowledge pages.\n"
                "- `index.json` is a stable page index for future document UIs.\n"
                "- `graph.json` is a stable node/edge graph interface.\n\n"
                "## Page Format\n\n"
                "Pages use Markdown with simple frontmatter. Cross-page references use "
                "`[[Page Title]]`; matching links are exported as `references` edges in "
                "`graph.json`.\n\n"
                "## Source Of Truth\n\n"
                "Markdown pages are the source of truth. JSON files are derived indexes "
                "and may be regenerated by cb-agent.\n",
                encoding="utf-8",
            )
        if not self.index_path.exists():
            _write_json(self.index_path, {"version": 1, "updated_at": _now_iso(), "pages": []})
        if not self.graph_path.exists():
            _write_json(self.graph_path, {"version": 1, "updated_at": _now_iso(), "nodes": [], "edges": []})

    def capture_turn(
        self,
        *,
        user_text: str,
        assistant_text: str,
        work_record_text: str = "",
        long_term_memory_path: Optional[Path] = None,
    ) -> KnowledgeCaptureResult:
        """Capture valuable long-term memory from one completed conversation turn.

        历史上这里还做过"知识页自动捕获"(靠 _looks_like_knowledge 启发式判断
        是否值得记，再 upsert_page)。该路径已移除，原因有二：
        1. 它依赖 work_record_text 文本，而 CC 对齐重构后该字段恒为空，启发式
           触发条件已失效，捕获质量退化；
        2. 它与 knowledge_write 工具职责重复——后者由模型基于语义主动判断"这值得
           记"，并整理成结构化正文，比字符长度启发式可靠得多，写入同一个
           KnowledgeBase。

        因此自动捕获只保留 MEMORY.md 长期记忆这一条(只依赖 user_text 的显式
        "请记住"类触发)，结构化知识页改由模型显式调用 knowledge_write 写入。
        ``work_record_text`` 参数保留仅为兼容调用方签名，现已不参与任何逻辑。
        """
        del work_record_text  # 不再参与捕获逻辑，保留形参仅为兼容签名
        result = KnowledgeCaptureResult()
        try:
            self.ensure_structure()
        except Exception as exc:
            result.errors.append(f"knowledge init failed: {exc}")
            return result

        if long_term_memory_path and self._looks_like_memory(user_text):
            try:
                self.append_long_term_memory(long_term_memory_path, user_text)
                result.memory_updated = True
            except Exception as exc:
                logger.exception("failed to append long-term memory")
                result.errors.append(f"long-term memory update failed: {exc}")

        return result

    def append_long_term_memory(self, path: Path, content: str) -> None:
        """Append a concise unique bullet to the configured MEMORY.md."""
        path = path.expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        bullet = "- " + _clip_text(" ".join(str(content).split()), 320)
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="replace")
        else:
            text = "# MEMORY\n\n## Captured memories\n"
        if bullet in text:
            return
        if "## Captured memories" not in text:
            text = text.rstrip() + "\n\n## Captured memories\n"
        text = text.rstrip() + f"\n{bullet}  \n  captured_at: {_now_iso()}\n"
        path.write_text(text + "\n", encoding="utf-8")

    def upsert_page(
        self,
        *,
        title: str,
        body: str,
        tags: Optional[Iterable[str]] = None,
        source: str = "manual",
        metadata: Optional[dict[str, Any]] = None,
    ) -> Path:
        self.ensure_structure()
        title = _clip_text(title or "Untitled knowledge", 80)
        slug = _stable_slug(title)
        page_path = self.pages_dir / f"{slug}.md"
        created_at = _now_iso()
        tags_list = sorted({str(t).strip() for t in (tags or []) if str(t).strip()})

        existing_meta: dict[str, Any] = {}
        existing_body = ""
        if page_path.exists():
            existing_meta, existing_body = _parse_simple_frontmatter(
                page_path.read_text(encoding="utf-8", errors="replace")
            )
            created_at = str(existing_meta.get("created_at") or created_at)

        body = self._add_cross_references(body, title)
        meta = {
            "id": slug,
            "title": title,
            "created_at": created_at,
            "updated_at": _now_iso(),
            "tags": sorted(set(tags_list + list(existing_meta.get("tags") or []))),
            "source": source,
            "namespace": self.namespace,
        }
        if metadata:
            meta.update(metadata)

        if existing_body:
            new_body = (
                existing_body.rstrip()
                + f"\n\n## Update {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                + body.strip()
                + "\n"
            )
        else:
            new_body = f"# {title}\n\n" + body.strip() + "\n"

        page_path.write_text(
            _format_frontmatter(meta) + "\n\n" + new_body,
            encoding="utf-8",
        )
        self.refresh_indexes()
        self._index_page_for_rag(page_path)
        return page_path

    def refresh_indexes(self) -> None:
        """Rebuild `index.json` and `graph.json` from Markdown pages."""
        self.ensure_structure()
        pages: list[dict[str, Any]] = []
        title_to_id: dict[str, str] = {}
        bodies: dict[str, str] = {}

        for page in sorted(self.pages_dir.glob("*.md")):
            meta, body = _parse_simple_frontmatter(
                page.read_text(encoding="utf-8", errors="replace")
            )
            title = str(meta.get("title") or page.stem)
            page_id = str(meta.get("id") or page.stem)
            record = {
                "id": page_id,
                "title": title,
                "path": str(page),
                "tags": meta.get("tags") or [],
                "created_at": meta.get("created_at"),
                "updated_at": meta.get("updated_at"),
                "source": meta.get("source"),
                "namespace": meta.get("namespace") or self.namespace,
                "summary": _clip_text(self._first_paragraph(body), 260),
            }
            pages.append(record)
            title_to_id[title] = page_id
            bodies[page_id] = body

        edges: list[dict[str, str]] = []
        for record in pages:
            body = bodies.get(record["id"], "")
            linked_titles = set(re.findall(r"\[\[([^\]]+)\]\]", body))
            for linked_title in sorted(linked_titles):
                target = title_to_id.get(linked_title)
                if target and target != record["id"]:
                    edges.append(
                        {"source": record["id"], "target": target, "type": "references"}
                    )

        _write_json(
            self.index_path,
            {"version": 1, "updated_at": _now_iso(), "pages": pages},
        )
        _write_json(
            self.graph_path,
            {
                "version": 1,
                "updated_at": _now_iso(),
                "nodes": [
                    {"id": p["id"], "label": p["title"], "path": p["path"], "tags": p["tags"]}
                    for p in pages
                ],
                "edges": edges,
            },
        )

    def read_index(self) -> dict[str, Any]:
        """Return the current page index for document-browser UIs."""
        self.ensure_structure()
        return _read_json(
            self.index_path,
            {"version": 1, "updated_at": _now_iso(), "pages": []},
        )

    def read_graph(self) -> dict[str, Any]:
        """Return the current graph payload for knowledge-graph UIs."""
        self.ensure_structure()
        return _read_json(
            self.graph_path,
            {"version": 1, "updated_at": _now_iso(), "nodes": [], "edges": []},
        )

    def render_related_context(
        self,
        query: str,
        *,
        limit: int = 5,
        max_chars: int = 3500,
    ) -> str:
        """Return relevant snippets from Markdown and optional RAG."""
        if not query or not query.strip():
            return ""
        try:
            self.ensure_structure()
        except Exception:
            return ""

        snippets: list[str] = []
        seen: set[str] = set()
        for item in self._lexical_search(query, limit=limit):
            seen.add(item["id"])
            snippets.append(
                f"## {item['title']}\n"
                f"source: {item['path']}\n"
                f"{item['snippet']}"
            )

        rag_text = self._search_rag(query, limit=limit)
        if rag_text:
            snippets.append("## Vector/RAG matches\n" + rag_text)

        text = "\n\n".join(snippets)
        return _clip_text(text, max_chars)

    def _lexical_search(self, query: str, *, limit: int) -> list[dict[str, str]]:
        index = _read_json(self.index_path, {"pages": []})
        query_terms = _word_terms(query)
        scored: list[tuple[float, dict[str, Any], str]] = []
        for record in index.get("pages", []):
            path = Path(record.get("path") or "")
            if not path.is_file():
                continue
            raw = path.read_text(encoding="utf-8", errors="replace")
            _meta, body = _parse_simple_frontmatter(raw)
            haystack = " ".join(
                [
                    str(record.get("title") or ""),
                    " ".join(record.get("tags") or []),
                    body[:4000],
                ]
            )
            terms = _word_terms(haystack)
            overlap = len(query_terms & terms)
            if overlap <= 0:
                continue
            title_bonus = 2 if any(t in str(record.get("title", "")).lower() for t in query_terms) else 0
            scored.append((overlap + title_bonus, record, body))

        scored.sort(key=lambda x: x[0], reverse=True)
        out: list[dict[str, str]] = []
        for _score, record, body in scored[:limit]:
            out.append(
                {
                    "id": str(record.get("id") or ""),
                    "title": str(record.get("title") or "Untitled"),
                    "path": str(record.get("path") or ""),
                    "snippet": _clip_text(self._best_snippet(body, query_terms), 700),
                }
            )
        return out

    def _get_rag_pipeline(self) -> Optional[dict[str, Any]]:
        if not self.enable_rag or self._rag_failed:
            return None
        if self._rag_pipeline is not None:
            return self._rag_pipeline
        try:
            from memory.rag.pipeline import create_rag_pipeline

            self._rag_pipeline = create_rag_pipeline(
                qdrant_url=os.getenv("QDRANT_URL"),
                qdrant_api_key=os.getenv("QDRANT_API_KEY"),
                collection_name=os.getenv(
                    "CBAGENT_KNOWLEDGE_COLLECTION",
                    "cbagent_knowledge_vectors",
                ),
                rag_namespace=f"knowledge:{self.namespace}",
            )
            return self._rag_pipeline
        except Exception as exc:
            self._rag_failed = True
            logger.debug("knowledge RAG unavailable: %s", exc)
            return None

    def _index_page_for_rag(self, page_path: Path) -> None:
        pipeline = self._get_rag_pipeline()
        if not pipeline:
            return
        try:
            pipeline["add_documents"]([str(page_path)], chunk_size=800, chunk_overlap=120)
        except Exception as exc:
            logger.debug("knowledge RAG indexing failed for %s: %s", page_path, exc)

    def _search_rag(self, query: str, *, limit: int) -> str:
        pipeline = self._get_rag_pipeline()
        if not pipeline:
            return ""
        try:
            results = pipeline["search"](query=query, top_k=limit)
        except Exception as exc:
            logger.debug("knowledge RAG search failed: %s", exc)
            return ""
        parts: list[str] = []
        for result in results or []:
            meta = result.get("metadata", {})
            content = meta.get("content") or result.get("content") or ""
            source = meta.get("source_path") or meta.get("doc_id") or result.get("id")
            if content:
                parts.append(
                    f"- source: {source}; score: {float(result.get('score', 0.0)):.3f}\n"
                    f"  {_clip_text(content, 500)}"
                )
        return "\n".join(parts)

    def _add_cross_references(self, body: str, current_title: str) -> str:
        index = _read_json(self.index_path, {"pages": []})
        related: list[str] = []
        lower_body = body.lower()
        for record in index.get("pages", []):
            title = str(record.get("title") or "")
            if not title or title == current_title:
                continue
            if title.lower() in lower_body:
                related.append(title)
        if not related:
            return body
        refs = "\n".join(f"- [[{title}]]" for title in sorted(set(related))[:10])
        return body.rstrip() + "\n\n## Related\n\n" + refs + "\n"

    def _looks_like_memory(self, text: str) -> bool:
        lower = (text or "").lower()
        return any(trigger in lower or trigger in text for trigger in MEMORY_TRIGGERS)

    def _first_paragraph(self, body: str) -> str:
        for part in body.split("\n\n"):
            part = part.strip()
            if part and not part.startswith("#"):
                return part
        return body.strip()

    def _best_snippet(self, body: str, terms: set[str]) -> str:
        paragraphs = [
            p.strip()
            for p in body.split("\n\n")
            if p.strip() and not p.lstrip().startswith("#")
        ]
        if not paragraphs:
            return body
        best = max(
            paragraphs,
            key=lambda p: len(_word_terms(p) & terms),
        )
        return best


__all__ = ["KnowledgeBase", "KnowledgeCaptureResult"]
