"""文件读取工具

最小可用的 FileRead：
- path: 文件路径（绝对/相对当前 cwd）
- 模式四选一：head / tail / range / char_range（默认 head）
- 输出最多 32KB，再超返回截断提示

设计目标是配合 BashTool 的输出落盘：模型拿到 output_file 后用
file_read(path=..., tail=200) 拉尾部，避免再起一次 bash 跑 tail。

读取成功后会向 ReadStateRegistry 登记 (path, mtime)，供 FileWriteTool
做 staleness check 用——避免覆盖 linter/用户在 read 之后做的并发改动。

不处理：二进制（默认 utf-8 解码 + replace 错误），多文件，glob，符号链接。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.tool import Tool, ToolParameter
from tools.tools.file_state import get_read_state_registry

#TODO: 1.固定死32kB有点不灵活，应该根据不同场景设置不同max
MAX_OUTPUT_BYTES = 32 * 1024  # 32KB


class FileReadTool(Tool):
    def __init__(self):
        super().__init__(
            name="file_read",
            description=(
                "读取文本文件。支持四种模式：head（前 N 行）、tail（后 N 行）、"
                "range（指定行号范围）、char_range（指定字符范围）。默认 head 100 行。"
                "用于查看 BashTool 落盘的命令输出（output_file 字段）、"
                "源代码片段、日志文件等。最大返回 32KB。"
                "在了解文件内容的情况下，请尽量使用 head/tail/range/start_char/end_char "
                "限制返回范围，避免返回过多内容。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="文件路径，绝对路径或相对当前工作目录。",
                required=True,
            ),
            ToolParameter(
                name="head",
                type="number",
                description="读前 N 行（默认 100）。与 tail / range / char_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="tail",
                type="number",
                description="读后 N 行。与 head / range / char_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="start_line",
                type="number",
                description="range 模式起始行号（1-based，含）。与 char_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="end_line",
                type="number",
                description="range 模式结束行号（1-based，含）。与 char_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="start_char",
                type="number",
                description=(
                    "char_range 模式起始字符位置（1-based，含）。"
                    "用于读取超长单行文件的中后段。"
                ),
                required=False,
            ),
            ToolParameter(
                name="end_char",
                type="number",
                description="char_range 模式结束字符位置（1-based，含）。",
                required=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> Any:
        if not isinstance(parameters, dict):
            return {"error": "参数必须是字典类型"}
        path = parameters.get("path")
        if not path or not isinstance(path, str):
            return {"error": "path 参数必须是有效的字符串"}
        for k in ("head", "tail", "start_line", "end_line", "start_char", "end_char"):
            v = parameters.get(k)
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                return {"error": f"{k} 参数必须是有效的非负数"}
        return {"error": None}

    def run(self, parameters: Dict[str, Any]) -> str:
        validation_result = self.validate_parameters(parameters)
        if validation_result["error"]:
            return json.dumps({"error": validation_result["error"]}, ensure_ascii=False)

        # .expanduser(): 将～转化为家目录
        p = Path(parameters["path"]).expanduser()
        if not p.is_absolute(): # is_absolute() 判断是否为绝对路径
            # 相对路径用 BashSession.cwd（与其它工具一致），导入放函数内避免循环依赖
            from tools.tools.bash_session import get_session
            p = Path(get_session().cwd) / p
        if not p.exists():
            return json.dumps(
                {"error": f"文件不存在: {p}", "content": ""},
                ensure_ascii=False,
            )
        if not p.is_file():
            return json.dumps(
                {"error": f"不是文件: {p}", "content": ""},
                ensure_ascii=False,
            )

        head: Optional[int] = self._to_int(parameters.get("head"))
        tail: Optional[int] = self._to_int(parameters.get("tail"))
        start: Optional[int] = self._to_int(parameters.get("start_line"))
        end: Optional[int] = self._to_int(parameters.get("end_line"))
        start_char: Optional[int] = self._to_int(parameters.get("start_char"))
        end_char: Optional[int] = self._to_int(parameters.get("end_char"))

        # 互斥校验
        modes_set = sum(1 for x in (head, tail, (start or end), (start_char or end_char)) if x)
        if modes_set > 1:
            return json.dumps(
                {"error": "head / tail / range / char_range 互斥，只能选一种模式"},
                ensure_ascii=False,
            )

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return json.dumps(
                {"error": f"读取失败: {e}", "content": str(e)},
                ensure_ascii=False,
            )

        # 给 FileWriteTool 的 staleness check 留下记录：路径 + 当时 mtime
        get_read_state_registry().mark_read(p)

        lines = text.splitlines()
        total_lines = len(lines)
        total_chars = len(text)
        char_range: Optional[Tuple[int, int]] = None

        if start_char or end_char:
            # 超长单行文件无法靠行号定位中间内容，因此字符模式按原文字符位置切片。
            cs = max(1, start_char or 1)
            ce = end_char or total_chars
            if ce < cs:
                return json.dumps(
                    {"error": "end_char 必须大于或等于 start_char", "content": ""},
                    ensure_ascii=False,
                )
            content = text[cs - 1:ce]
            selected = content.splitlines()
            char_range = (cs, min(ce, total_chars))
            mode = f"char_range-{cs}-{ce}"
        elif tail:
            selected = lines[-tail:]
            mode = f"tail-{tail}"
        elif start or end:
            s = max(1, start or 1)
            e = end or total_lines
            selected = lines[s - 1: e]
            mode = f"range-{s}-{e}"
        else:
            n = head or 100
            selected = lines[:n]
            mode = f"head-{n}"

        if char_range is None:
            content = "\n".join(selected)

        # 尾部读取遇到超长单行时应保留结尾；其它模式保留窗口开头并提示继续用字符范围读取。
        content, truncated = self._truncate_to_output_limit(
            content,
            keep_tail=bool(tail),
        )

        return json.dumps(
            {
                "path": str(p),
                "mode": mode,
                "total_lines": total_lines,
                "total_chars": total_chars,
                "returned_lines": len(selected),
                "returned_chars": len(content),
                "char_range": char_range,
                "truncated": truncated,
                "content": content,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _to_int(v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            iv = int(v)
            return iv if iv > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truncate_to_output_limit(content: str, *, keep_tail: bool = False) -> Tuple[str, bool]:
        """
        截断 content 到 MAX_OUTPUT_BYTES 字节以内，返回截断后的内容和是否发生了截断。
        """
        encoded = content.encode("utf-8")
        if len(encoded) <= MAX_OUTPUT_BYTES:
            return content, False

        marker = f"\n... [超过 {MAX_OUTPUT_BYTES} 字节已截断，可用 start_char/end_char 继续分段读取] ..."
        marker_bytes = marker.encode("utf-8")
        budget = max(0, MAX_OUTPUT_BYTES - len(marker_bytes))

        if keep_tail:
            # tail 模式的语义是看结尾，超限时保留末尾字节并在前面放截断提示。
            clipped = encoded[-budget:] if budget else b""
            clipped_text = clipped.decode("utf-8", errors="ignore")
            return marker.lstrip() + "\n" + clipped_text, True

        clipped = encoded[:budget] if budget else b""
        clipped_text = clipped.decode("utf-8", errors="ignore")
        return clipped_text + marker, True
