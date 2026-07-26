"""MemoryLoader —— 多级 CLAUDE.md 加载与合并。

对应 claude-code/src/utils/claudemd.ts 中 getMemoryFiles。

加载顺序(数组靠后 = 优先级高 = 在 system prompt 中位置靠后):
1. Managed: %ProgramData%\\cb-agent\\CLAUDE.md / /etc/cb-agent/CLAUDE.md
2. User: ~/.cbagent/CLAUDE.md + ~/.cbagent/rules/*.md
3. Project: 从根向 cwd 逐层 CLAUDE.md / .claude/CLAUDE.md /
   .cbagent/CLAUDE.md / .{claude,cbagent}/rules/*.md
4. Local: $cwd/CLAUDE.local.md

每个文件再递归处理 @include,深度上限 5,循环检测用 resolved path set。

异步 memoize: 缓存 Future。并发 caller 共享同一次解析,避免风暴。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Optional

from .frontmatter import parse_frontmatter, strip_block_html_comments
from .include_resolver import extract_include_paths
from .paths import (
    get_knowledge_root,
    get_local_memory_path,
    get_managed_memory_path,
    get_managed_rules_dir,
    get_short_term_memory_path,
    get_user_core_memory_path,
    get_user_memory_path,
    get_user_rules_dir,
    iter_user_core_memory_paths,
    iter_project_memory_candidates,
    iter_rules_dir,
)
from .types import MemoryFileInfo, MemoryType


logger = logging.getLogger(__name__)


MAX_INCLUDE_DEPTH = 5
MAX_MEMORY_CHARACTER_COUNT = 40_000
MAX_FILE_BYTES = 256 * 1024  # 单个文件正文纳入预算的软上限
# 为扫描 @include 允许读到的硬上限；超过则明确失败，禁止静默截断导致尾部 include 丢失。
MAX_INCLUDE_SCAN_BYTES = 2 * 1024 * 1024


class MemoryBudgetError(RuntimeError):
    """Managed 指令无法完整纳入预算时抛出，调用方应阻止本次请求。"""


class MemoryReadError(RuntimeError):
    """已发现的 memory 文件无法可靠读取或完整扫描。"""


def _safe_read_text(path: Path) -> str:
    """读 markdown 全文（含 @include 扫描所需内容）。

    不再在 include 解析前静默截断 256KB：超大文件要么完整读入（≤2MB），
    要么返回 None 并打日志，由上层标记失败。正文是否进入 prompt 由预算层决定。
    """
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise MemoryReadError(f"memory 文件读取失败: {path}: {error}") from error
    if len(raw) > MAX_INCLUDE_SCAN_BYTES:
        logger.error(
            "memory 文件超过 include 扫描上限 (%s bytes > %s): %s",
            len(raw),
            MAX_INCLUDE_SCAN_BYTES,
            path,
        )
        raise MemoryReadError(
            f"memory 文件超过 include 扫描上限: {path} "
            f"({len(raw)} > {MAX_INCLUDE_SCAN_BYTES} bytes)"
        )
    return raw.decode("utf-8", errors="replace")


async def _process_memory_file(
    path: Path,
    file_type: MemoryType,
    *,
    processed: set[str],
    depth: int = 0,
    parent: Optional[Path] = None,
    included_via: str = "",
) -> List[MemoryFileInfo]:
    """递归处理一个 memory 文件 + 它的所有 @include。

    顺序: 父文件先入 list,递归后的 include 文件后入。这样 include 在
    数组里位置靠后 -> system prompt 中位置靠后 -> 模型更"重视"。
    """
    if depth >= MAX_INCLUDE_DEPTH:
        logger.debug("memory @include depth limit reached at %s", path)
        return []
    try:
        resolved = path.resolve()
    except OSError as error:
        raise MemoryReadError(f"memory 路径解析失败: {path}: {error}") from error
    key = str(resolved)
    if key in processed:
        return []
    processed.add(key)

    raw = await asyncio.to_thread(_safe_read_text, resolved)
    meta, body = parse_frontmatter(raw)
    body = strip_block_html_comments(body)
    if not body.strip():
        return []
    info = MemoryFileInfo(
        path=resolved,
        type=file_type,
        content=body,
        parent=parent,
        included_via=included_via,
        frontmatter=meta,
    )
    out: List[MemoryFileInfo] = [info]

    # 提取 @include 并递归
    includes = await asyncio.to_thread(extract_include_paths, body, resolved)
    for inc in includes:
        if not inc.exists():
            continue
        # @include 子文件继承父类型 -> 防止 User CLAUDE.md include 项目文件
        # 时优先级被错误降为 Project。
        sub = await _process_memory_file(
            inc,
            file_type,
            processed=processed,
            depth=depth + 1,
            parent=resolved,
            included_via=str(inc),
        )
        out.extend(sub)
    return out


class _AsyncMemoize:
    """简易 async memoize: 缓存 Future,并发 caller 共享一次解析。"""

    def __init__(self, fn: Callable[..., Awaitable]) -> None:
        self._fn = fn
        self._cache: dict[tuple, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def __call__(self, *args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        async with self._lock:
            fut = self._cache.get(key)
            if fut is None:
                loop = asyncio.get_running_loop()
                fut = loop.create_future()
                self._cache[key] = fut
                kick_off = True
            else:
                kick_off = False
        if kick_off:
            try:
                result = await self._fn(*args, **kwargs)
                fut.set_result(result)
            except Exception as e:
                fut.set_exception(e)
                # 失败的 future 不要被永久 cache,清掉以便下次重试
                async with self._lock:
                    self._cache.pop(key, None)
                raise
        return await fut

    def clear(self) -> None:
        # 注意: 不取消已经在 await 的 caller。它们继续等待原 Future 即可。
        self._cache.clear()


class MemoryLoader:
    """多级 CLAUDE.md 加载器。

    一个 session 通常持有一个实例。get_memory_files 是 memoized 入口,
    /clear 与 /compact 调 reset_cache 让缓存失效。
    """

    MAX_INCLUDE_DEPTH = MAX_INCLUDE_DEPTH
    MAX_MEMORY_CHARACTER_COUNT = MAX_MEMORY_CHARACTER_COUNT

    def __init__(
        self,
        cwd: Path,
        *,
        include_managed: bool = True,
        include_user: bool = True,
        include_knowledge: bool = True,
    ) -> None:
        self.cwd = cwd.resolve()
        self.include_managed = include_managed
        self.include_user = include_user
        self.include_knowledge = include_knowledge
        self.knowledge_root = get_knowledge_root(self.cwd)
        namespace_digest = hashlib.sha1(
            str(self.knowledge_root).lower().encode("utf-8", errors="ignore")
        ).hexdigest()[:12]
        self.knowledge_namespace = "workspace:" + namespace_digest
        self._knowledge_base = None
        self._knowledge_context_disabled_reason = ""
        self._last_budget_report: Optional[MemoryBudgetReport] = None
        self._memo = _AsyncMemoize(self._compute_memory_files)

    def get_last_budget_report(self) -> Optional[MemoryBudgetReport]:
        """最近一次预算裁剪报告（含 omitted/truncated，供 manifest 注入）。"""
        return self._last_budget_report

    async def get_memory_files(self) -> List[MemoryFileInfo]:
        """返回当前会话的 memory 文件列表(已合并、去重、深度受限)。"""
        return await self._memo()

    def get_knowledge_base(self):
        """Return the lazily-created structured knowledge base."""
        if self._knowledge_base is None:
            from .knowledge import KnowledgeBase

            self._knowledge_base = KnowledgeBase(
                self.knowledge_root,
                namespace=self.knowledge_namespace,
            )
        return self._knowledge_base

    async def get_knowledge_context(
        self,
        query: str,
        *,
        limit: int = 5,
        max_chars: int = 3500,
    ) -> str:
        """Retrieve related structured knowledge for the current prompt."""
        if not self.include_knowledge or not query or not str(query).strip():
            return ""
        if self._knowledge_context_disabled_reason:
            logger.debug(
                "knowledge context skipped: %s",
                self._knowledge_context_disabled_reason,
            )
            return ""
        try:
            started = time.perf_counter()
            kb = self.get_knowledge_base()
            timeout_raw = os.getenv("CBAGENT_KNOWLEDGE_CONTEXT_TIMEOUT", "3")
            try:
                timeout = float(timeout_raw)
            except ValueError:
                timeout = 3.0
            task = asyncio.to_thread(
                kb.render_related_context,
                str(query),
                limit=limit,
                max_chars=max_chars,
            )
            if timeout > 0:
                result = await asyncio.wait_for(task, timeout=timeout)
            else:
                result = await task
            elapsed = time.perf_counter() - started
            logger.info(
                "knowledge context built: chars=%s elapsed=%.2fs rag=%s root=%s",
                len(result or ""),
                elapsed,
                getattr(kb, "enable_rag", None),
                self.knowledge_root,
            )
            return result
        except asyncio.TimeoutError:
            self._knowledge_context_disabled_reason = (
                "retrieval timed out; restart or raise "
                "CBAGENT_KNOWLEDGE_CONTEXT_TIMEOUT to retry"
            )
            logger.warning(
                "knowledge context retrieval timed out after %ss; disabled for this session",
                os.getenv("CBAGENT_KNOWLEDGE_CONTEXT_TIMEOUT", "3"),
            )
            return ""
        except Exception:
            logger.exception("knowledge context retrieval failed")
            return ""

    def record_turn(
        self,
        *,
        user_text: str,
        assistant_text: str,
        work_record_text: str = "",
    ):
        """Best-effort memory and knowledge update after a completed turn."""
        if not self.include_knowledge:
            return None
        try:
            kb = self.get_knowledge_base()
            result = kb.capture_turn(
                user_text=user_text,
                assistant_text=assistant_text,
                work_record_text=work_record_text,
                long_term_memory_path=get_user_core_memory_path("MEMORY.md"),
            )
            if getattr(result, "memory_updated", False):
                self.reset_cache(reason="record_turn_memory_update")
            return result
        except Exception:
            logger.exception("turn memory/knowledge capture failed")
            return None

    def reset_cache(self, reason: str = "session_start") -> None:
        """清空 memoize 缓存。

        触发时机: /clear、/compact、settings 热更新、cwd 切换。
        reason 仅用于调试日志。
        """
        logger.debug("MemoryLoader.reset_cache reason=%s", reason)
        self._memo.clear()

    async def _compute_memory_files(self) -> List[MemoryFileInfo]:
        """实际加载逻辑。Managed -> User -> Project -> Local 顺序。"""
        processed: set[str] = set()
        out: List[MemoryFileInfo] = []

        async def _add_file(p: Path, t: MemoryType) -> None:
            if not p.is_file():
                return
            chunk = await _process_memory_file(p, t, processed=processed)
            out.extend(chunk)

        async def _add_dir(d: Path, t: MemoryType) -> None:
            if not d.is_dir():
                return
            for md in iter_rules_dir(d):
                await _add_file(md, t)

        if self.include_managed:
            await _add_file(get_managed_memory_path(), "Managed")
            await _add_dir(get_managed_rules_dir(), "Managed")
        if self.include_user:
            await _add_file(get_user_memory_path(), "Global")
            for core_path in iter_user_core_memory_paths():
                await _add_file(core_path, "Global")
            await _add_dir(get_user_rules_dir(), "User")
        # Project 层: 根 -> cwd 逐层
        for path, _label in iter_project_memory_candidates(self.cwd):
            await _process_then_extend(out, path, "Project", processed)
        # Local 层
        await _add_file(get_short_term_memory_path(self.cwd), "ShortTerm")
        await _add_file(get_local_memory_path(self.cwd), "Local")

        # 总字符数上限：按类型优先级，计入 formatter 开销；禁止 Local 挤掉 Managed。
        budgeted = enforce_memory_budget(out, self.MAX_MEMORY_CHARACTER_COUNT)
        # omitted/truncated 元数据挂在 loader 上，供 formatter 生成 manifest。
        self._last_budget_report = budgeted
        return budgeted.selected


async def _process_then_extend(
    sink: List[MemoryFileInfo],
    path: Path,
    file_type: MemoryType,
    processed: set[str],
) -> None:
    chunk = await _process_memory_file(path, file_type, processed=processed)
    sink.extend(chunk)


# 数字越小优先级越高。同一 type 内，原列表靠后 = 更靠近 cwd（Project 加载顺序根→cwd）。
_TYPE_PRIORITY: dict[str, int] = {
    "Managed": 0,
    "User": 1,
    "Global": 1,
    "Project": 2,
    "ShortTerm": 3,
    "Local": 3,
    "Knowledge": 4,
}


@dataclass
class MemoryBudgetReport:
    """一次预算裁剪结果。"""

    selected: List[MemoryFileInfo]
    omitted: List[MemoryFileInfo]
    truncated: List[tuple[MemoryFileInfo, str]]  # (原文件, preview 正文)
    used_chars: int
    limit: int


def _formatter_entry_overhead(path: Path, file_type: str) -> int:
    """估算 format_memory_files 为单文件追加的标题开销（路径 + 标签 + 换行）。"""
    from .formatter import _TYPE_LABEL

    label = _TYPE_LABEL.get(file_type, "instructions")  # type: ignore[arg-type]
    # 与 formatter 模板一致: "\nContents of {path} ({label}):\n\n{content}"
    return len(f"\nContents of {path} ({label}):\n\n")


def _prompt_overhead() -> int:
    from .formatter import MEMORY_INSTRUCTION_PROMPT

    return len(MEMORY_INSTRUCTION_PROMPT)


def enforce_memory_budget(
    files: Iterable[MemoryFileInfo],
    limit: int,
) -> MemoryBudgetReport:
    """按固定优先级把 memory 文件装入字符预算。

    规则：
    1. Managed 必须完整纳入；装不下则抛 MemoryBudgetError（阻止请求）。
    2. User/Global 优先完整纳入；装不下则整文件 omitted（禁止无提示截断指令）。
    3. Project：更靠近 cwd 的优先；装不下则 omitted。
    4. ShortTerm/Local：可注入有来源的 preview，其余 omitted 并记入 manifest。
    5. 预算计入 formatter 标题与 MEMORY_INSTRUCTION_PROMPT 开销。
    """
    from dataclasses import replace

    files = list(files)
    if limit <= 0:
        return MemoryBudgetReport(selected=files, omitted=[], truncated=[], used_chars=0, limit=limit)

    # 排序键：优先级升序，同优先级保留原相对顺序（Project 靠后 = 更近 cwd 优先用 stable sort 反转索引）
    indexed = list(enumerate(files))
    indexed.sort(
        key=lambda item: (
            _TYPE_PRIORITY.get(item[1].type, 99),
            # 同 type 内原列表靠后优先（更靠近 cwd 的 Project）
            -item[0],
        )
    )

    remaining = max(0, int(limit) - _prompt_overhead())
    selected_map: dict[int, MemoryFileInfo] = {}
    omitted: List[MemoryFileInfo] = []
    truncated: List[tuple[MemoryFileInfo, str]] = []

    for original_index, info in indexed:
        overhead = _formatter_entry_overhead(info.path, info.type)
        content = info.content or ""
        # 单文件正文硬上限：超过则先裁到 preview，指令类仍视为“无法完整纳入”
        oversized = len(content.encode("utf-8", errors="replace")) > MAX_FILE_BYTES
        if oversized and info.type in {"Managed", "User", "Global", "Project"}:
            # 指令文件不允许静默截断：整文件 omitted，Managed 另外走硬错误。
            if info.type == "Managed":
                raise MemoryBudgetError(
                    f"Managed 指令文件过大无法完整注入（>{MAX_FILE_BYTES} bytes）: {info.path}"
                )
            omitted.append(info)
            continue

        if oversized:
            # ShortTerm/Local：落 preview
            preview = content[: max(0, min(len(content), remaining - overhead - 80))]
            preview = (
                preview
                + f"\n\n... [文件超过 {MAX_FILE_BYTES} bytes，已省略后续；完整内容见 {info.path}] ..."
            )
            cost = overhead + len(preview)
            if cost > remaining and selected_map:
                omitted.append(info)
                continue
            selected_map[original_index] = replace(info, content=preview)
            truncated.append((info, preview))
            remaining = max(0, remaining - cost)
            continue

        cost = overhead + len(content)
        if cost <= remaining:
            selected_map[original_index] = info
            remaining -= cost
            continue

        # 装不下
        if info.type == "Managed":
            raise MemoryBudgetError(
                f"Managed 指令无法完整纳入 {limit} 字符预算: {info.path} "
                f"(need={cost}, remaining={remaining})。请拆分 Managed 指令后重试。"
            )
        if info.type in {"User", "Global", "Project"}:
            omitted.append(info)
            continue
        # ShortTerm/Local：有预算则 preview，否则 omit
        if remaining <= overhead + 64:
            omitted.append(info)
            continue
        body_budget = remaining - overhead - 80
        if body_budget <= 0:
            omitted.append(info)
            continue
        preview = content[:body_budget] + (
            f"\n\n... [预算不足已截断 preview；完整文件: {info.path}] ..."
        )
        selected_map[original_index] = replace(info, content=preview)
        truncated.append((info, preview))
        remaining = max(0, remaining - (overhead + len(preview)))

    # 按原加载顺序输出 selected，保持 Prompt 结构稳定
    selected = [selected_map[i] for i in sorted(selected_map)]
    used = limit - remaining if limit > 0 else 0
    # used 重算为实际选中内容 + 开销
    used = _prompt_overhead()
    for f in selected:
        used += _formatter_entry_overhead(f.path, f.type) + len(f.content or "")
    return MemoryBudgetReport(
        selected=selected,
        omitted=omitted,
        truncated=truncated,
        used_chars=used,
        limit=limit,
    )


__all__ = [
    "MemoryLoader",
    "MemoryBudgetError",
    "MemoryReadError",
    "MemoryBudgetReport",
    "MAX_INCLUDE_DEPTH",
    "MAX_MEMORY_CHARACTER_COUNT",
    "MAX_FILE_BYTES",
    "enforce_memory_budget",
]
