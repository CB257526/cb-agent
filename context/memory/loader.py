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
import logging
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Optional

from .frontmatter import parse_frontmatter, strip_block_html_comments
from .include_resolver import extract_include_paths
from .paths import (
    get_local_memory_path,
    get_managed_memory_path,
    get_managed_rules_dir,
    get_user_memory_path,
    get_user_rules_dir,
    iter_project_memory_candidates,
    iter_rules_dir,
)
from .types import MemoryFileInfo, MemoryType


logger = logging.getLogger(__name__)


MAX_INCLUDE_DEPTH = 5
MAX_MEMORY_CHARACTER_COUNT = 40_000
MAX_FILE_BYTES = 256 * 1024  # 单个文件 256KB 上限


def _safe_read_text(path: Path) -> Optional[str]:
    """读 markdown 文件,失败返回 None。

    限制单文件大小,避免一个超大文件撑爆 system prompt。
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        raw = raw[:MAX_FILE_BYTES]
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return None


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
    except OSError:
        return []
    key = str(resolved)
    if key in processed:
        return []
    processed.add(key)

    raw = await asyncio.to_thread(_safe_read_text, resolved)
    if raw is None:
        return []
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
    ) -> None:
        self.cwd = cwd.resolve()
        self.include_managed = include_managed
        self.include_user = include_user
        self._memo = _AsyncMemoize(self._compute_memory_files)

    async def get_memory_files(self) -> List[MemoryFileInfo]:
        """返回当前会话的 memory 文件列表(已合并、去重、深度受限)。"""
        return await self._memo()

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
            await _add_file(get_user_memory_path(), "User")
            await _add_dir(get_user_rules_dir(), "User")
        # Project 层: 根 -> cwd 逐层
        for path, _label in iter_project_memory_candidates(self.cwd):
            await _process_then_extend(out, path, "Project", processed)
        # Local 层
        await _add_file(get_local_memory_path(self.cwd), "Local")

        # 总字符数上限保护(对齐 claude-code MAX_MEMORY_CHARACTER_COUNT)
        return _enforce_total_char_limit(out, self.MAX_MEMORY_CHARACTER_COUNT)


async def _process_then_extend(
    sink: List[MemoryFileInfo],
    path: Path,
    file_type: MemoryType,
    processed: set[str],
) -> None:
    chunk = await _process_memory_file(path, file_type, processed=processed)
    sink.extend(chunk)


def _enforce_total_char_limit(
    files: Iterable[MemoryFileInfo],
    limit: int,
) -> List[MemoryFileInfo]:
    """从尾部(优先级高)往头部累加,超过 limit 的尾段被丢弃。

    保留高优先级 -> Local/Project cwd 文件先纳入预算,远祖目录的可能被截掉。
    """
    files = list(files)
    if limit <= 0:
        return files
    total = 0
    keep_from_tail: List[MemoryFileInfo] = []
    for f in reversed(files):
        size = len(f.content)
        if total + size > limit and keep_from_tail:
            break
        keep_from_tail.append(f)
        total += size
    keep_from_tail.reverse()
    return keep_from_tail


__all__ = ["MemoryLoader", "MAX_INCLUDE_DEPTH", "MAX_MEMORY_CHARACTER_COUNT"]
