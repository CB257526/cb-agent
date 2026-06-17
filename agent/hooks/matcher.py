"""Hook matcher 匹配规则。

对齐 Claude Code 的三段式 matcher 语义：matcher 写法决定如何与事件字段
（工具事件是 tool_name，压缩事件是 reason 等）比较：

- ``""`` / ``"*"`` / None        → 全匹配，事件每次发生都命中
- 仅含 ``[A-Za-z0-9_|]`` 字符    → 精确串，或 ``|`` 分隔的精确串列表
                                   （如 ``"bash|file_edit"`` 命中其一即可）
- 含其它字符（``.`` ``^`` ``*`` 等）→ 当作正则，用 ``re.search`` 部分匹配
                                   （如 ``"mcp__.*"`` 命中所有 MCP 工具）

之所以用「字符集」而非「先试精确再试正则」来区分，是为了让 ``bash|file_edit``
这种竖线列表稳定走精确分支，不会被误当成正则的「或」——两者结果虽常一致，
但精确分支零正则开销，也避免用户写错时静默退化成正则。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# 精确/竖线列表分支允许的字符集：字母、数字、下划线、竖线。
_EXACT_CHARS = re.compile(r"^[A-Za-z0-9_|]+$")


def matches(matcher: Optional[str], value: str) -> bool:
    """判断 matcher 是否命中 value。

    Args:
        matcher: hooks.json 里配置的 matcher 字符串（可能为 None）
        value: 事件用于匹配的字段值（工具事件传 tool_name）
    Returns:
        是否命中
    """
    # 全匹配：未配置、空串、星号
    if matcher is None:
        return True
    matcher = matcher.strip()
    if matcher == "" or matcher == "*":
        return True

    value = value or ""

    # 精确串或竖线分隔列表
    if _EXACT_CHARS.match(matcher):
        parts = [p for p in matcher.split("|") if p]
        return value in parts

    # 其余当作正则。正则非法时不命中（记 warning，避免坏配置静默全匹配）
    try:
        return re.search(matcher, value) is not None
    except re.error:
        logger.warning("hook matcher 正则非法，已跳过: matcher=%r", matcher)
        return False


__all__ = ["matches"]
