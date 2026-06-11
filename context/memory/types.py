"""Memory 数据类型。

对应 claude-code 中 MemoryFile / MemoryFileType 等定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional


MemoryType = Literal[
    "Managed",
    "User",
    "Global",
    "Project",
    "ShortTerm",
    "Local",
    "Knowledge",
]


@dataclass
class MemoryFileInfo:
    """一个加载完成的 CLAUDE.md(或 rules/*.md)文件。

    fields:
        path: 绝对路径(已 resolve)。
        type: 来源层级,用于 formatter 标注 "(project instructions, ...)"。
        content: frontmatter 与块级 HTML 注释剥离后的正文。
        parent: @include 链中触发本文件加载的父文件;顶层加载为 None。
        included_via: 触发加载的具体 @include 字符串(用于调试)。
    """

    path: Path
    type: MemoryType
    content: str
    parent: Optional[Path] = None
    included_via: str = ""
    frontmatter: dict = field(default_factory=dict)


__all__ = ["MemoryFileInfo", "MemoryType"]
