"""本地搜索与导航工具。

本模块实现三个面向代码库定位的只读工具：
- glob：按文件名 / 通配符定位文件
- grep：按文件内容 / 正则定位代码
- ls：浏览目录结构

设计上优先调用系统 ``rg``，因为 ripgrep 对大型仓库、.gitignore 和文件遍历的
性能都更好；当本机没有安装 ``rg`` 时，自动降级为 Python 文件遍历，保证工具
仍然可用。降级路径会复用同一套忽略规则和输出格式，避免模型因为后端变化拿到
完全不同形状的数据。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import time
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from tools.tool import Tool, ToolParameter


# 这些目录默认不参与搜索。它们要么是版本控制元数据，要么是依赖、构建产物或
# 缓存目录；把它们排除可以显著降低 token 噪声，也避免在 node_modules 等目录里
# 做没有价值的海量扫描。
DEFAULT_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".svn",
        ".hg",
        ".bzr",
        ".jj",
        ".sl",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

_extra_ignore_dirs: ContextVar[frozenset[str]] = ContextVar(
    "cbagent_local_search_extra_ignore_dirs",
    default=frozenset(),
)

RG_TIMEOUT_SECONDS = 20.0
GLOB_DEFAULT_LIMIT = 100
GREP_DEFAULT_HEAD_LIMIT = 250
LS_DEFAULT_LIMIT = 200
MAX_LIMIT = 5000


def _json(data: Dict[str, Any]) -> str:
    """统一 JSON 输出格式，保留中文错误信息。"""
    return json.dumps(data, ensure_ascii=False)


def _json_error(message: str, **extra: Any) -> str:
    payload = {"error": message}
    payload.update(extra)
    return _json(payload)


def _session_cwd() -> Path:
    """读取与 file_read/file_write 相同的当前工作目录。

    导入放在函数内，避免工具模块加载时就初始化 BashSession；这样测试也可以通过
    reset_session(initial_cwd=...) 精确控制相对路径基准。
    """
    from tools.tools.bash_session import get_session

    return Path(get_session().cwd).expanduser().resolve()


def _resolve_user_path(raw_path: Optional[str]) -> Path:
    """把工具参数里的 path 解析成绝对路径。

    约定与 FileReadTool 保持一致：绝对路径原样使用，相对路径基于当前
    BashSession.cwd。这里不做“必须位于项目内”的限制，因为 cb-agent 现有文件
    工具也允许用户明确读取绝对路径。
    """
    base = _session_cwd()
    if not raw_path:
        return base
    path = Path(str(raw_path)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _display_path(path: Path) -> str:
    """返回给模型的路径尽量相对当前 cwd，减少 token 占用。"""
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(_session_cwd())
        text = "." if str(relative) == "." else relative.as_posix()
    except ValueError:
        text = str(resolved)
    return text.replace("\\", "/")


def _path_from_rg(cwd: Path, value: str) -> Path:
    """把 ripgrep 输出的相对路径还原为绝对 Path。"""
    cleaned = value.strip()
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    path = Path(cleaned)
    return path if path.is_absolute() else (cwd / path).resolve()


def _coerce_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int = MAX_LIMIT,
) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("必须是整数") from exc
    if number < minimum or number > maximum:
        raise ValueError(f"必须位于 {minimum}-{maximum} 之间")
    return number


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError("必须是布尔值")


def set_search_ignore_dirs(names: Iterable[str]) -> Token[frozenset[str]]:
    """为当前 Agent 上下文追加搜索忽略目录，并传播到 ToolExecutor 线程。"""

    additions = frozenset(str(name) for name in names if str(name))
    return _extra_ignore_dirs.set(_extra_ignore_dirs.get() | additions)


def reset_search_ignore_dirs(token: Token[frozenset[str]]) -> None:
    """恢复进入当前 Agent 前的额外搜索忽略目录。"""

    _extra_ignore_dirs.reset(token)


def _effective_ignore_dirs() -> frozenset[str]:
    return DEFAULT_IGNORE_DIRS | _extra_ignore_dirs.get()


def _is_ignored_dir(name: str) -> bool:
    return name in _effective_ignore_dirs()


def _iter_files(root: Path, *, include_hidden: bool = True) -> Iterable[Path]:
    """Python 降级搜索的文件遍历器。

    os.walk 会原地修改 dirs 列表；我们在这里剔除默认忽略目录和可选隐藏目录，避免
    后续递归进入这些分支。符号链接目录不递归，防止循环链接把搜索带出预期范围。
    """
    if root.is_file():
        if include_hidden or not root.name.startswith("."):
            yield root
        return

    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [
            name
            for name in dirs
            if not _is_ignored_dir(name)
            and (include_hidden or not name.startswith("."))
        ]
        for name in files:
            if not include_hidden and name.startswith("."):
                continue
            yield Path(current) / name


def _split_glob_patterns(raw: Optional[str]) -> List[str]:
    """拆分 glob 参数，支持空格/逗号分隔，同时保留 ``*.{py,ts}`` 这类花括号。"""
    if not raw:
        return []
    patterns: List[str] = []
    buf: List[str] = []
    brace_depth = 0
    for ch in str(raw):
        if ch == "{":
            brace_depth += 1
        elif ch == "}" and brace_depth:
            brace_depth -= 1

        if brace_depth == 0 and (ch.isspace() or ch == ","):
            if buf:
                patterns.append("".join(buf).strip())
                buf.clear()
            continue
        buf.append(ch)

    if buf:
        patterns.append("".join(buf).strip())
    return [item.replace("\\", "/") for item in patterns if item]


def _matches_single_glob(relative_path: str, name: str, pattern: str) -> bool:
    normalized = pattern.replace("\\", "/")
    # ``**/*.py`` 在很多工具里也被理解为“任意层级的 py 文件”，包括当前
    # 目录下的 a.py；fnmatch 对这个语义不完全一致，所以额外补一个去掉
    # ``**/`` 前缀的匹配分支，让 Python 降级结果更贴近 ripgrep。
    if normalized.startswith("**/") and _matches_single_glob(
        relative_path,
        name,
        normalized[3:],
    ):
        return True
    if "/" not in normalized:
        # ripgrep 的 --glob "*.py" 会匹配任意层级的 py 文件；Python 的
        # fnmatch 不区分路径分隔符，所以这里显式同时比对文件名和相对路径。
        return fnmatch.fnmatchcase(name, normalized) or fnmatch.fnmatchcase(
            relative_path,
            normalized,
        )
    return fnmatch.fnmatchcase(relative_path, normalized)


def _matches_any_glob(path: Path, base: Path, patterns: Sequence[str]) -> bool:
    if not patterns:
        return True
    try:
        relative_path = path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        relative_path = path.name
    return any(_matches_single_glob(relative_path, path.name, item) for item in patterns)


def _rg_ignore_args() -> List[str]:
    args: List[str] = []
    for name in sorted(_effective_ignore_dirs()):
        args.extend(["--glob", f"!{name}/**"])
        args.extend(["--glob", f"!**/{name}/**"])
    return args


def _run_rg(args: Sequence[str], cwd: Path) -> Tuple[Optional[List[str]], Optional[str], str]:
    """执行 ripgrep。

    返回三元组 ``(lines, error, backend)``：
    - lines 为 None 表示 rg 不可用，调用方应降级到 Python
    - error 非空表示 rg 已执行但失败，应把错误返回给模型
    - backend 用于结果里标识当前后端，方便排查性能差异
    """
    rg = shutil.which("rg")
    if not rg:
        return None, None, "python"

    try:
        completed = subprocess.run(
            [rg, *args],
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=RG_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return None, None, "python"
    except subprocess.TimeoutExpired:
        return [], f"ripgrep 超时（{RG_TIMEOUT_SECONDS:.0f}s），请缩小 path 或 pattern", "rg"
    except OSError as exc:
        return [], f"ripgrep 执行失败: {exc}", "rg"

    if completed.returncode in (0, 1):
        lines = [line.rstrip("\r") for line in completed.stdout.splitlines() if line]
        return lines, None, "rg"

    stderr = " ".join((completed.stderr or "").split())
    return [], f"ripgrep 返回错误码 {completed.returncode}: {stderr}", "rg"


def _sort_by_mtime(paths: Sequence[Path]) -> List[Path]:
    def key(path: Path) -> Tuple[float, str]:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (-mtime, _display_path(path).lower())

    return sorted(paths, key=key)


def _slice_page(
    items: Sequence[Any],
    *,
    head_limit: int,
    offset: int,
) -> Tuple[List[Any], bool, Optional[int], int]:
    """统一分页语义。

    head_limit=0 代表显式不限量；其它情况先跳过 offset，再返回 head_limit 条。
    applied_limit/applied_offset 总是回传，便于模型继续分页。
    """
    start = min(offset, len(items))
    if head_limit == 0:
        return list(items[start:]), False, None, offset
    end = start + head_limit
    sliced = list(items[start:end])
    truncated = len(items) > end
    return sliced, truncated, head_limit, offset


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _glob_with_python(pattern: str, base: Path) -> List[Path]:
    return [
        path.resolve()
        for path in _iter_files(base, include_hidden=True)
        if _matches_any_glob(path, base, [pattern])
    ]


def _glob_files(pattern: str, base: Path) -> Tuple[List[Path], str, Optional[str]]:
    lines, error, backend = _run_rg(
        ["--files", "--hidden", "--glob", pattern, *_rg_ignore_args()],
        base,
    )
    if lines is None:
        return _glob_with_python(pattern, base), backend, None
    if error:
        return [], backend, error
    return [_path_from_rg(base, line) for line in lines], backend, None


def _parse_count_line(line: str, cwd: Path, target: str) -> Optional[Tuple[Path, int]]:
    idx = line.rfind(":")
    if idx > 0:
        path_part = line[:idx]
        count_part = line[idx + 1 :]
    else:
        path_part = target
        count_part = line
    try:
        count = int(count_part.strip())
    except ValueError:
        return None
    return _path_from_rg(cwd, path_part), count


_RG_CONTENT_LINE_RE = re.compile(
    r"^(?P<path>.*?)(?P<sep1>[:\-])(?P<line>\d+)(?P<sep2>[:\-])(?P<rest>.*)$"
)


def _format_rg_content_line(line: str, cwd: Path) -> Tuple[str, Optional[str]]:
    """把 rg content 输出中的路径改为相对 cwd 的路径。

    开启 context 时，ripgrep 会输出 ``path-line-context`` 和 ``--`` 分隔行；
    正则同时兼容冒号与短横线分隔，不匹配的行原样返回。
    """
    if line == "--":
        return line, None
    match = _RG_CONTENT_LINE_RE.match(line)
    if not match:
        return line, None
    display = _display_path(_path_from_rg(cwd, match.group("path")))
    formatted = (
        f"{display}{match.group('sep1')}{match.group('line')}"
        f"{match.group('sep2')}{match.group('rest')}"
    )
    return formatted, display


class GlobTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="glob",
            description=(
                "按文件名或通配符快速查找本地文件。支持 **/*.py、src/**/*.ts "
                "等 glob pattern；结果按最近修改时间排序。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="pattern",
                type="string",
                description="用于匹配文件名的 glob pattern，例如 **/*.py 或 src/**/*.ts。",
                required=True,
            ),
            ToolParameter(
                name="path",
                type="string",
                description="搜索目录；省略时使用当前工作目录。",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description=f"最大返回文件数，1-{MAX_LIMIT}。",
                required=False,
                default=GLOB_DEFAULT_LIMIT,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        try:
            pattern = parameters.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return False
            path = parameters.get("path")
            if path is not None and not isinstance(path, str):
                return False
            _coerce_int(
                parameters.get("limit"),
                default=GLOB_DEFAULT_LIMIT,
                minimum=1,
            )
            return True
        except ValueError:
            return False

    def run(self, parameters: Dict[str, Any]) -> str:
        start = time.perf_counter()
        if not self.validate_parameters(parameters):
            return _json_error("参数验证失败")

        base = _resolve_user_path(parameters.get("path"))
        if not base.exists():
            return _json_error("目录不存在", path=str(base), duration_ms=_duration_ms(start))
        if not base.is_dir():
            return _json_error("path 必须是目录", path=str(base), duration_ms=_duration_ms(start))

        limit = _coerce_int(parameters.get("limit"), default=GLOB_DEFAULT_LIMIT, minimum=1)
        pattern = str(parameters["pattern"]).strip().replace("\\", "/")

        files, backend, error = _glob_files(pattern, base)
        if error:
            return _json_error(error, path=str(base), backend=backend, duration_ms=_duration_ms(start))

        sorted_files = _sort_by_mtime(files)
        sliced, truncated, _applied_limit, _applied_offset = _slice_page(
            sorted_files,
            head_limit=limit,
            offset=0,
        )
        return _json(
            {
                "path": str(base),
                "backend": backend,
                "duration_ms": _duration_ms(start),
                "truncated": truncated,
                "files": [_display_path(path) for path in sliced],
                "num_files": len(sliced),
                "total_matches": len(sorted_files),
            }
        )


class GrepTool(Tool):
    OUTPUT_MODES = {"files_with_matches", "content", "count"}

    def __init__(self) -> None:
        super().__init__(
            name="grep",
            description=(
                "使用正则搜索本地文件内容。优先用此工具做代码搜索，不要通过 bash "
                "调用 rg/grep；支持文件 glob 过滤、大小写开关、上下文行和分页。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="pattern",
                type="string",
                description="要搜索的正则表达式；以 - 开头的 pattern 也会被安全处理。",
                required=True,
            ),
            ToolParameter(
                name="path",
                type="string",
                description="搜索文件或目录；省略时使用当前工作目录。",
                required=False,
            ),
            ToolParameter(
                name="glob",
                type="string",
                description='文件过滤 glob，例如 "*.py"、"**/*.tsx"；多个 pattern 可用逗号或空格分隔。',
                required=False,
            ),
            ToolParameter(
                name="output_mode",
                type="string",
                description="输出模式：files_with_matches、content 或 count。",
                required=False,
                default="files_with_matches",
            ),
            ToolParameter(
                name="head_limit",
                type="integer",
                description="最大返回条目数；0 表示不限量，默认 250。",
                required=False,
                default=GREP_DEFAULT_HEAD_LIMIT,
            ),
            ToolParameter(
                name="offset",
                type="integer",
                description="跳过前 N 条结果，用于继续分页。",
                required=False,
                default=0,
            ),
            ToolParameter(
                name="case_insensitive",
                type="boolean",
                description="是否忽略大小写。",
                required=False,
                default=False,
            ),
            ToolParameter(
                name="context",
                type="integer",
                description="content 模式下每个匹配前后展示的上下文行数。",
                required=False,
                default=0,
            ),
            ToolParameter(
                name="multiline",
                type="boolean",
                description="是否启用跨行正则匹配。",
                required=False,
                default=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        try:
            pattern = parameters.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return False
            path = parameters.get("path")
            if path is not None and not isinstance(path, str):
                return False
            glob_value = parameters.get("glob")
            if glob_value is not None and not isinstance(glob_value, str):
                return False
            mode = str(parameters.get("output_mode") or "files_with_matches")
            if mode not in self.OUTPUT_MODES:
                return False
            _coerce_int(
                parameters.get("head_limit"),
                default=GREP_DEFAULT_HEAD_LIMIT,
                minimum=0,
            )
            _coerce_int(parameters.get("offset"), default=0, minimum=0)
            _coerce_int(parameters.get("context"), default=0, minimum=0)
            _coerce_bool(parameters.get("case_insensitive"), default=False)
            _coerce_bool(parameters.get("multiline"), default=False)
            return True
        except ValueError:
            return False

    def run(self, parameters: Dict[str, Any]) -> str:
        start = time.perf_counter()
        if not self.validate_parameters(parameters):
            return _json_error("参数验证失败")

        target_path = _resolve_user_path(parameters.get("path"))
        if not target_path.exists():
            return _json_error("path 不存在", path=str(target_path), duration_ms=_duration_ms(start))

        mode = str(parameters.get("output_mode") or "files_with_matches")
        head_limit = _coerce_int(
            parameters.get("head_limit"),
            default=GREP_DEFAULT_HEAD_LIMIT,
            minimum=0,
        )
        offset = _coerce_int(parameters.get("offset"), default=0, minimum=0)
        context = _coerce_int(parameters.get("context"), default=0, minimum=0)
        case_insensitive = _coerce_bool(parameters.get("case_insensitive"), default=False)
        multiline = _coerce_bool(parameters.get("multiline"), default=False)
        glob_patterns = _split_glob_patterns(parameters.get("glob"))

        result, backend, error = self._grep(
            pattern=str(parameters["pattern"]),
            target_path=target_path,
            mode=mode,
            glob_patterns=glob_patterns,
            head_limit=head_limit,
            offset=offset,
            case_insensitive=case_insensitive,
            context=context,
            multiline=multiline,
        )
        if error:
            return _json_error(error, path=str(target_path), backend=backend, duration_ms=_duration_ms(start))

        result.update(
            {
                "path": str(target_path),
                "backend": backend,
                "duration_ms": _duration_ms(start),
            }
        )
        return _json(result)

    def _grep(
        self,
        *,
        pattern: str,
        target_path: Path,
        mode: str,
        glob_patterns: Sequence[str],
        head_limit: int,
        offset: int,
        case_insensitive: bool,
        context: int,
        multiline: bool,
    ) -> Tuple[Dict[str, Any], str, Optional[str]]:
        cwd = target_path.parent if target_path.is_file() else target_path
        target = target_path.name if target_path.is_file() else "."

        args: List[str] = ["--hidden", "--max-columns", "500"]
        if case_insensitive:
            args.append("-i")
        if multiline:
            args.extend(["-U", "--multiline-dotall"])
        if mode == "files_with_matches":
            args.append("-l")
        elif mode == "count":
            # 强制带文件名，避免单文件搜索时 rg 只返回 ``3``，让 count/content
            # 模式的解析规则在“目录”和“单文件”两种场景下保持一致。
            # 使用 --count-matches 而不是 -c：-c 统计“匹配行数”，这里的
            # num_matches 语义是“匹配次数”，同一行出现两次也应记为 2。
            args.extend(["--with-filename", "--count-matches"])
        else:
            args.extend(["--with-filename", "-n"])
            if context:
                args.extend(["-C", str(context)])
        for item in glob_patterns:
            args.extend(["--glob", item])
        # ripgrep 的 glob 规则按顺序覆盖；忽略项必须放在用户 include 之后，
        # 否则 `**/*.py` 会重新把 node_modules/.cbagent 等目录纳入搜索。
        args.extend(_rg_ignore_args())
        if pattern.startswith("-"):
            args.extend(["-e", pattern])
        else:
            args.append(pattern)
        args.append(target)

        lines, error, backend = _run_rg(args, cwd)
        if lines is None:
            return self._grep_python(
                pattern=pattern,
                target_path=target_path,
                mode=mode,
                glob_patterns=glob_patterns,
                head_limit=head_limit,
                offset=offset,
                case_insensitive=case_insensitive,
                context=context,
                multiline=multiline,
            )
        if error:
            return {}, backend, error
        return self._format_rg_result(
            lines=lines,
            cwd=cwd,
            target=target,
            mode=mode,
            head_limit=head_limit,
            offset=offset,
        ), backend, None

    def _format_rg_result(
        self,
        *,
        lines: Sequence[str],
        cwd: Path,
        target: str,
        mode: str,
        head_limit: int,
        offset: int,
    ) -> Dict[str, Any]:
        if mode == "files_with_matches":
            paths = _sort_by_mtime([_path_from_rg(cwd, line) for line in lines])
            sliced, truncated, applied_limit, applied_offset = _slice_page(
                paths,
                head_limit=head_limit,
                offset=offset,
            )
            files = [_display_path(path) for path in sliced]
            return {
                "mode": mode,
                "truncated": truncated,
                "files": files,
                "content": "",
                "num_files": len(files),
                "num_matches": 0,
                "applied_limit": applied_limit,
                "applied_offset": applied_offset,
            }

        if mode == "count":
            parsed = [item for item in (_parse_count_line(line, cwd, target) for line in lines) if item]
            sliced, truncated, applied_limit, applied_offset = _slice_page(
                parsed,
                head_limit=head_limit,
                offset=offset,
            )
            count_lines = [f"{_display_path(path)}:{count}" for path, count in sliced]
            return {
                "mode": mode,
                "truncated": truncated,
                "files": [_display_path(path) for path, _count in sliced],
                "content": "\n".join(count_lines),
                "num_files": len(sliced),
                "num_matches": sum(count for _path, count in sliced),
                "applied_limit": applied_limit,
                "applied_offset": applied_offset,
            }

        sliced, truncated, applied_limit, applied_offset = _slice_page(
            list(lines),
            head_limit=head_limit,
            offset=offset,
        )
        formatted_lines: List[str] = []
        files_seen: List[str] = []
        seen_set = set()
        for line in sliced:
            formatted, file_path = _format_rg_content_line(line, cwd)
            formatted_lines.append(formatted)
            if file_path and file_path not in seen_set:
                seen_set.add(file_path)
                files_seen.append(file_path)
        return {
            "mode": mode,
            "truncated": truncated,
            "files": files_seen,
            "content": "\n".join(formatted_lines),
            "num_files": len(files_seen),
            "num_matches": len(formatted_lines),
            "applied_limit": applied_limit,
            "applied_offset": applied_offset,
        }

    def _grep_python(
        self,
        *,
        pattern: str,
        target_path: Path,
        mode: str,
        glob_patterns: Sequence[str],
        head_limit: int,
        offset: int,
        case_insensitive: bool,
        context: int,
        multiline: bool,
    ) -> Tuple[Dict[str, Any], str, Optional[str]]:
        flags = re.IGNORECASE if case_insensitive else 0
        if multiline:
            flags |= re.DOTALL
        try:
            regex = re.compile(pattern, flags)
        except re.error as exc:
            return {}, "python", f"正则表达式无效: {exc}"

        base = target_path.parent if target_path.is_file() else target_path
        candidates = [
            path
            for path in _iter_files(target_path, include_hidden=True)
            if _matches_any_glob(path, base, glob_patterns)
        ]

        matched_files: List[Path] = []
        count_entries: List[Tuple[Path, int]] = []
        content_entries: List[str] = []

        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if multiline:
                matches = list(regex.finditer(text))
                match_line_indexes = [
                    text.count("\n", 0, match.start())
                    for match in matches
                ]
                lines = text.splitlines()
                count = len(matches)
            else:
                lines = text.splitlines()
                match_line_indexes = [
                    index for index, line in enumerate(lines) if regex.search(line)
                ]
                count = sum(len(list(regex.finditer(line))) for line in lines)

            if count <= 0:
                continue

            matched_files.append(path)
            count_entries.append((path, count))

            if mode == "content":
                selected = set()
                for index in match_line_indexes:
                    start = max(0, index - context)
                    end = min(len(lines), index + context + 1)
                    selected.update(range(start, end))
                for index in sorted(selected):
                    content_entries.append(f"{_display_path(path)}:{index + 1}:{lines[index]}")

        if mode == "files_with_matches":
            sorted_files = _sort_by_mtime(matched_files)
            sliced, truncated, applied_limit, applied_offset = _slice_page(
                sorted_files,
                head_limit=head_limit,
                offset=offset,
            )
            files = [_display_path(path) for path in sliced]
            return {
                "mode": mode,
                "truncated": truncated,
                "files": files,
                "content": "",
                "num_files": len(files),
                "num_matches": 0,
                "applied_limit": applied_limit,
                "applied_offset": applied_offset,
            }, "python", None

        if mode == "count":
            sliced, truncated, applied_limit, applied_offset = _slice_page(
                count_entries,
                head_limit=head_limit,
                offset=offset,
            )
            return {
                "mode": mode,
                "truncated": truncated,
                "files": [_display_path(path) for path, _count in sliced],
                "content": "\n".join(f"{_display_path(path)}:{count}" for path, count in sliced),
                "num_files": len(sliced),
                "num_matches": sum(count for _path, count in sliced),
                "applied_limit": applied_limit,
                "applied_offset": applied_offset,
            }, "python", None

        sliced, truncated, applied_limit, applied_offset = _slice_page(
            content_entries,
            head_limit=head_limit,
            offset=offset,
        )
        files = []
        seen = set()
        for line in sliced:
            match = _RG_CONTENT_LINE_RE.match(line)
            if match:
                file_path = match.group("path")
                if file_path not in seen:
                    seen.add(file_path)
                    files.append(file_path)
        return {
            "mode": mode,
            "truncated": truncated,
            "files": files,
            "content": "\n".join(sliced),
            "num_files": len(files),
            "num_matches": len(sliced),
            "applied_limit": applied_limit,
            "applied_offset": applied_offset,
        }, "python", None


class LsTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="ls",
            description=(
                "浏览本地目录结构，返回紧凑的文件树列表。适合先了解项目布局，"
                "再配合 glob/grep 精确定位文件或代码。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="要浏览的目录；省略时使用当前工作目录。",
                required=False,
            ),
            ToolParameter(
                name="depth",
                type="integer",
                description="递归深度，0 表示不展开子项，默认 1。",
                required=False,
                default=1,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description=f"最大返回条目数，1-{MAX_LIMIT}。",
                required=False,
                default=LS_DEFAULT_LIMIT,
            ),
            ToolParameter(
                name="include_hidden",
                type="boolean",
                description="是否展示以 . 开头的隐藏文件/目录。",
                required=False,
                default=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        try:
            path = parameters.get("path")
            if path is not None and not isinstance(path, str):
                return False
            _coerce_int(parameters.get("depth"), default=1, minimum=0, maximum=20)
            _coerce_int(parameters.get("limit"), default=LS_DEFAULT_LIMIT, minimum=1)
            _coerce_bool(parameters.get("include_hidden"), default=False)
            return True
        except ValueError:
            return False

    def run(self, parameters: Dict[str, Any]) -> str:
        start = time.perf_counter()
        if not self.validate_parameters(parameters):
            return _json_error("参数验证失败")

        root = _resolve_user_path(parameters.get("path"))
        if not root.exists():
            return _json_error("目录不存在", path=str(root), duration_ms=_duration_ms(start))
        if not root.is_dir():
            return _json_error("path 必须是目录", path=str(root), duration_ms=_duration_ms(start))

        depth = _coerce_int(parameters.get("depth"), default=1, minimum=0, maximum=20)
        limit = _coerce_int(parameters.get("limit"), default=LS_DEFAULT_LIMIT, minimum=1)
        include_hidden = _coerce_bool(parameters.get("include_hidden"), default=False)

        entries, total_entries = self._list_entries(
            root=root,
            depth=depth,
            limit=limit,
            include_hidden=include_hidden,
        )
        return _json(
            {
                "path": str(root),
                "duration_ms": _duration_ms(start),
                "truncated": total_entries > len(entries),
                "entries": entries,
                "total_entries": total_entries,
            }
        )

    def _list_entries(
        self,
        *,
        root: Path,
        depth: int,
        limit: int,
        include_hidden: bool,
    ) -> Tuple[List[str], int]:
        entries: List[str] = []
        total = 0

        def walk(directory: Path, current_depth: int) -> None:
            nonlocal total
            if current_depth >= depth:
                return
            try:
                children = list(directory.iterdir())
            except OSError:
                return
            children.sort(key=lambda item: (not item.is_dir(), item.name.lower()))
            for child in children:
                if _is_ignored_dir(child.name):
                    continue
                if not include_hidden and child.name.startswith("."):
                    continue

                is_dir = child.is_dir()
                total += 1
                if len(entries) < limit:
                    suffix = "/" if is_dir else ""
                    entries.append(f"{_display_path(child)}{suffix}")
                if is_dir and not child.is_symlink():
                    walk(child, current_depth + 1)

        walk(root, 0)
        return entries, total


__all__ = [
    "GlobTool",
    "GrepTool",
    "LsTool",
    "reset_search_ignore_dirs",
    "set_search_ignore_dirs",
]
