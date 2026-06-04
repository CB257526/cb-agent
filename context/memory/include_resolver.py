"""@include 指令递归解析。

对应 claude-code 中 extractIncludePathsFromTokens / processMemoryFile 的核心逻辑。

@include 指令格式:
    @path/to/file              -> 相对当前 cwd
    @./relative/path.md        -> 相对 base 文件所在目录
    @~/home-relative.md        -> 用户 home 展开
    @/absolute/path.md         -> 绝对路径

提取规则:
- 跳过 ``` ... ``` 三反引号围栏代码块内的 @xxx
- 跳过行内 `code` span 内的 @xxx
- HTML 块注释由 frontmatter.strip_block_html_comments 提前剥离
- @ 前不能紧跟反斜杠(转义)
- 结尾的标点(. , ! ? ; : ) 等)不计入路径
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

# 行首或非 \ 字符之后的 @, 后接非空白(允许转义空格 \空格)
# 用 lookbehind 排除 \\@ 转义。Python re 不支持变长 lookbehind,
# 但 (?<=^|[^\\]) 是定长 0/1,所以分两个 alt 写。
INCLUDE_RE = re.compile(
    r"(?:(?<=^)|(?<=[^\\]))@((?:\\ |[^\s])+)",
    re.MULTILINE,
)

# 路径末尾的"明显不属于路径"的标点,逐字符剥
_TRAILING_PUNCT = ".,!?;:)]}>'\""


def _strip_fenced_and_inline_code(text: str) -> str:
    """把围栏代码块与行内 code span 替换为等长空格,保留行号一致以便定位。

    替换为空格(而非删除)的原因: regex 还要在剩余文本上找 @include,
    保留行结构便于调试时定位"原文哪一行触发了 include"。
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        # 三反引号 fenced
        if text.startswith("```", i):
            end = text.find("```", i + 3)
            if end == -1:
                # 未闭合,把剩余全部当代码
                out.append(" " * (n - i))
                i = n
                break
            # 包含开尾 ``` 三对反引号自身
            out.append(" " * (end + 3 - i))
            # 但保留换行符,否则 MULTILINE 行号会错乱
            out_chunk = text[i:end + 3]
            out[-1] = "".join(c if c == "\n" else " " for c in out_chunk)
            i = end + 3
            continue
        # 行内 `code`
        if text[i] == "`":
            end = text.find("`", i + 1)
            if end == -1:
                out.append(text[i])
                i += 1
                continue
            out.append("".join(c if c == "\n" else " " for c in text[i:end + 1]))
            i = end + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _strip_trailing_punct(s: str) -> str:
    while s and s[-1] in _TRAILING_PUNCT:
        s = s[:-1]
    return s


def _resolve_include_path(raw: str, base_dir: Path) -> Path:
    """把 @include 字符串解析为绝对 Path。

    raw 已经 strip 掉转义反斜杠(`\\ ` -> ` `)。
    """
    raw = raw.strip()
    raw = _strip_trailing_punct(raw)
    if not raw:
        return base_dir
    # 去除 fragment(`#section` 等)
    raw = raw.split("#", 1)[0]
    if not raw:
        return base_dir
    if raw.startswith("~"):
        return Path(raw).expanduser().resolve()
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    if raw.startswith("./") or raw.startswith("../"):
        return (base_dir / raw).resolve()
    # 默认相对 base_dir
    return (base_dir / raw).resolve()


def extract_include_paths(content: str, base_path: Path) -> List[Path]:
    """从 markdown 内容中提取所有 @include 引用,返回去重后的绝对路径列表。

    base_path 是当前文件所在目录(不是文件本身)。
    """
    base_dir = base_path if base_path.is_dir() else base_path.parent
    cleaned = _strip_fenced_and_inline_code(content)
    seen: set[str] = set()
    result: List[Path] = []
    for match in INCLUDE_RE.finditer(cleaned):
        raw = match.group(1).replace("\\ ", " ")
        target = _resolve_include_path(raw, base_dir)
        key = str(target)
        if key in seen:
            continue
        seen.add(key)
        result.append(target)
    return result


__all__ = ["extract_include_paths"]
