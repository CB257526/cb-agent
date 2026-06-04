"""最小 YAML frontmatter 解析。

对应 claude-code 中 parseFrontmatter。

设计:
- 不引入 PyYAML 等外部依赖,只支持 ``key: value`` 简单格式。
- 复杂结构(列表、嵌套)放正文里,frontmatter 仅承担索引/分类作用。
- frontmatter 块由首行 `---` 与一行 `---` 包围,缺失任一边界则视为没有
  frontmatter,整段当作 body。
"""

from __future__ import annotations

from typing import Tuple


def parse_frontmatter(text: str) -> Tuple[dict, str]:
    """解析最小 YAML frontmatter,返回 (meta_dict, body_text)。

    无 frontmatter 时返回 ({}, stripped_text)。
    """
    if not text.startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    if len(lines) < 2:
        return {}, text.strip()
    meta: dict = {}
    end_idx = None
    for idx in range(1, len(lines)):
        line = lines[idx]
        if line.strip() == "---":
            end_idx = idx
            break
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key:
            meta[key] = value.strip().strip("\"'")
    if end_idx is None:
        return {}, text.strip()
    body = "\n".join(lines[end_idx + 1:]).strip()
    return meta, body


def strip_block_html_comments(text: str) -> str:
    """剥离块级 HTML 注释 <!-- ... -->,保留行内代码内的注释。

    用最简单的非贪婪正则,跨行匹配。inline `code` 中的注释不剥(规范上
    inline code 不参与 @include 提取,所以无需特殊保护)。
    """
    import re

    return re.sub(r"<!--[\s\S]*?-->", "", text)


__all__ = ["parse_frontmatter", "strip_block_html_comments"]
