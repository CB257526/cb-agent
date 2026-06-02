"""轻量级 Markdown 记忆提供器。

这个模块是旧 RAG/向量记忆系统的轻量替代路径，设计目标很明确：

- 只依赖标准库和 ``utils.common`` 的 token 工具，不 import ``memory`` 包；
- 只读写 Markdown 文件，不需要 embedding、向量数据库、Qdrant API 或额外 env；
- 同时支持项目级记忆和用户全局记忆，运行时可直接把相关片段注入 ContextBuilder；
- 不注册新的记忆工具。用户要求“记住某事”时，由 system prompt 指引模型使用
  现有 file_read/file_write 去修改 MEMORY.md 或具体记忆文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from utils.common import count_tokens, jaccard, tokenize_for_relevance


MEMORY_INDEX_NAME = "MEMORY.md"
MEMORY_TYPES = {"user", "feedback", "project", "reference"}
INDEX_TEMPLATE = """# Memory Index

这个文件是 cb-agent 轻量 Markdown 记忆索引。具体记忆请写入同目录下的 .md 文件，
并在这里保留一行链接，便于 agent 快速判断哪些记忆值得读取。

示例：
- [用户偏好](user_preferences.md) — 用户偏好的工具、语言、回答风格
- [项目约定](project_conventions.md) — 当前项目的架构、命令、注意事项
"""


def default_global_memory_dir() -> Path:
    """返回用户全局 Markdown 记忆目录。"""
    return Path.home() / ".cbagent" / "memory"


def default_project_memory_dir(project_root: Path | str) -> Path:
    """返回项目级 Markdown 记忆目录。"""
    return Path(project_root).resolve() / ".cbagent" / "memory"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _safe_read_text(path: Path, limit_bytes: int = 128 * 1024) -> str:
    """读取 Markdown 文件，并限制最大读取量。

    轻量记忆是 prompt 输入源，必须避免一个超大 md 文件把上下文撑爆。这里按
    bytes 截断到 128KB，后续 ContextBuilder 还会按 token 预算再次筛选。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    truncated = raw[:limit_bytes]
    return truncated.decode("utf-8", errors="replace")


def _parse_frontmatter(text: str) -> tuple[Dict[str, str], str]:
    """解析最小 YAML frontmatter。

    为了不增加 PyYAML 以外的新依赖，也避免把轻量记忆绑定到完整 YAML 语法，这里
    只支持 ``key: value`` 这种简单格式。复杂内容可以放正文里，frontmatter 只承担
    索引和分类作用。
    """
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    meta: Dict[str, str] = {}
    end_idx: Optional[int] = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
        if ":" not in lines[idx]:
            continue
        key, value = lines[idx].split(":", 1)
        key = key.strip().lower()
        if key:
            meta[key] = value.strip().strip("\"'")
    if end_idx is None:
        return {}, text.strip()
    body = "\n".join(lines[end_idx + 1:]).strip()
    return meta, body


def _extract_index_links(index_text: str) -> Dict[str, str]:
    """从 MEMORY.md 中提取 ``[title](file.md) — description`` 索引行。"""
    links: Dict[str, str] = {}
    pattern = re.compile(r"\[[^\]]+\]\(([^)]+\.md)\)\s*(?:[-—:：]\s*(.*))?", re.IGNORECASE)
    for line in index_text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        filename = match.group(1).strip()
        if "/" in filename or "\\" in filename:
            continue
        links[filename] = (match.group(2) or "").strip()
    return links


@dataclass
class MarkdownMemoryItem:
    """一条 Markdown 记忆文件的轻量表示。"""

    scope: str
    path: Path
    name: str
    description: str
    memory_type: str
    body: str
    index_description: str = ""
    updated_at: datetime = field(default_factory=_now_utc)

    @property
    def searchable_text(self) -> str:
        return "\n".join(
            part for part in (
                self.name,
                self.description,
                self.index_description,
                self.memory_type,
                self.body,
            ) if part
        )

    def to_context(self, max_chars: int = 1200) -> str:
        rel = self.path.name
        header = (
            f"[{self.scope}/{self.memory_type}] {self.name} "
            f"({rel}) — {self.description or self.index_description}"
        ).strip()
        body = _clip(self.body, max_chars)
        return f"{header}\n{body}" if body else header


@dataclass
class MarkdownMemoryResult:
    """ContextBuilder 可消费的 Markdown 记忆召回结果。"""

    state_text: str = ""
    related_text: str = ""
    items: List[MarkdownMemoryItem] = field(default_factory=list)


class MarkdownMemoryProvider:
    """扫描并召回项目级/全局 Markdown 记忆。"""

    def __init__(
        self,
        *,
        project_dir: Path | str,
        global_dir: Optional[Path | str] = None,
        project_memory_dir: Optional[Path | str] = None,
        max_files: int = 80,
        max_related: int = 5,
        max_state: int = 6,
    ) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.global_dir = Path(global_dir).expanduser().resolve() if global_dir else default_global_memory_dir()
        self.project_memory_dir = (
            Path(project_memory_dir).resolve()
            if project_memory_dir
            else default_project_memory_dir(self.project_dir)
        )
        self.max_files = max_files
        self.max_related = max_related
        self.max_state = max_state

    def ensure_initialized(self) -> None:
        """创建两级记忆目录和默认 MEMORY.md。

        这里只创建模板文件，不写入任何业务事实，避免首次启动就制造“伪记忆”。
        """
        for directory in (self.global_dir, self.project_memory_dir):
            directory.mkdir(parents=True, exist_ok=True)
            index = directory / MEMORY_INDEX_NAME
            if not index.exists():
                index.write_text(INDEX_TEMPLATE, encoding="utf-8")

    def memory_instructions(self) -> str:
        """生成注入 system prompt 的轻量记忆写入说明。"""
        return (
            "【轻量 Markdown 记忆】\n"
            f"- 用户全局记忆目录：{self.global_dir}\n"
            f"- 当前项目记忆目录：{self.project_memory_dir}\n"
            "- 每个目录的 MEMORY.md 是索引文件，具体记忆写在同目录其他 .md 文件。\n"
            "- 记忆文件建议包含 frontmatter：name、description、type=user|feedback|project|reference、scope=global|project。\n"
            "- 当用户明确要求“记住/保存偏好/保存项目事实”时，先用 file_read 读取相关 MEMORY.md 或目标记忆文件，再用 file_write 更新。\n"
            "- 用户长期偏好写入全局目录；只对当前仓库有效的事实、约定、进展写入项目目录。\n"
            "- 不要凭空写入记忆；只保存用户明确要求保存或对后续任务明显必要的信息。"
        )

    def recall(self, query: str) -> MarkdownMemoryResult:
        """按当前问题召回 Markdown 记忆。"""
        items = self.scan()
        if not items:
            return MarkdownMemoryResult()

        state_items = self._state_items(items)
        related_items = self._related_items(items, query)
        return MarkdownMemoryResult(
            state_text=self._format_items(state_items, "Markdown 记忆状态"),
            related_text=self._format_items(related_items, "Markdown 相关记忆"),
            items=related_items,
        )

    def scan(self) -> List[MarkdownMemoryItem]:
        """扫描 global + project 两级记忆目录。"""
        self.ensure_initialized()
        items: List[MarkdownMemoryItem] = []
        items.extend(self._scan_dir(self.global_dir, "global"))
        items.extend(self._scan_dir(self.project_memory_dir, "project"))
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return items[: self.max_files]

    def _scan_dir(self, directory: Path, scope: str) -> List[MarkdownMemoryItem]:
        index_text = _safe_read_text(directory / MEMORY_INDEX_NAME, limit_bytes=32 * 1024)
        index_links = _extract_index_links(index_text)
        files = [
            path for path in directory.glob("*.md")
            if path.name != MEMORY_INDEX_NAME and path.is_file()
        ]
        # 索引文件中出现过的记忆优先，其余 md 也会被扫描，避免忘记维护索引时完全失效。
        files.sort(key=lambda p: (0 if p.name in index_links else 1, p.name.lower()))

        items: List[MarkdownMemoryItem] = []
        for path in files[: self.max_files]:
            text = _safe_read_text(path)
            if not text.strip():
                continue
            meta, body = _parse_frontmatter(text)
            try:
                updated = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                updated = _now_utc()
            memory_type = str(meta.get("type") or "reference").lower()
            if memory_type not in MEMORY_TYPES:
                memory_type = "reference"
            items.append(MarkdownMemoryItem(
                scope=scope,
                path=path,
                name=meta.get("name") or path.stem.replace("_", " "),
                description=meta.get("description") or "",
                memory_type=memory_type,
                body=body,
                index_description=index_links.get(path.name, ""),
                updated_at=updated,
            ))
        return items

    def _state_items(self, items: Sequence[MarkdownMemoryItem]) -> List[MarkdownMemoryItem]:
        """选出默认进入 [State] 的高价值记忆。"""
        preferred = [
            item for item in items
            if item.scope == "project" or item.memory_type in {"user", "feedback", "project"}
        ]
        return preferred[: self.max_state]

    def _related_items(self, items: Sequence[MarkdownMemoryItem], query: str) -> List[MarkdownMemoryItem]:
        if not query:
            return []
        query_tokens = tokenize_for_relevance(query)
        scored: List[tuple[float, MarkdownMemoryItem]] = []
        for item in items:
            content_tokens = tokenize_for_relevance(item.searchable_text)
            token_score = jaccard(query_tokens, content_tokens)
            keyword_score = self._keyword_overlap(query, item.searchable_text)
            score = max(token_score, keyword_score)
            if score <= 0:
                continue
            scored.append((score, item))
        scored.sort(key=lambda pair: (pair[0], pair[1].updated_at.timestamp()), reverse=True)
        return [item for _, item in scored[: self.max_related]]

    @staticmethod
    def _keyword_overlap(query: str, text: str) -> float:
        """一个很小的关键词兜底，弥补短中文 query 的 token 交集偏低问题。"""
        words = {
            w.lower()
            for w in re.findall(r"[A-Za-z0-9_\-]{2,}|[\u4e00-\u9fff]{2,}", query)
        }
        if not words:
            return 0.0
        haystack = text.lower()
        hits = sum(1 for word in words if word in haystack)
        return hits / max(len(words), 1)

    @staticmethod
    def _format_items(items: Iterable[MarkdownMemoryItem], title: str) -> str:
        chunks = [item.to_context() for item in items]
        chunks = [chunk for chunk in chunks if chunk.strip()]
        if not chunks:
            return ""
        return f"{title}：\n" + "\n\n".join(chunks)


__all__ = [
    "MarkdownMemoryItem",
    "MarkdownMemoryProvider",
    "MarkdownMemoryResult",
    "default_global_memory_dir",
    "default_project_memory_dir",
]
