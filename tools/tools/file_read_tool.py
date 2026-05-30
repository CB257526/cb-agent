"""文件读取工具

最小可用的 FileRead：
- path: 文件路径（绝对/相对当前 cwd）
- 模式三选一：head / tail / range（默认 head）
- 输出最多 100KB，再超返回截断提示

设计目标是配合 BashTool 的输出落盘：模型拿到 output_file 后用
file_read(path=..., tail=200) 拉尾部，避免再起一次 bash 跑 tail。

不处理：二进制（默认 utf-8 解码 + replace 错误），多文件，glob，符号链接。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter


MAX_OUTPUT_BYTES = 100 * 1024  # 100KB


class FileReadTool(Tool):
    def __init__(self):
        super().__init__(
            name="file_read",
            description=(
                "读取文本文件。支持三种模式：head（前 N 行）、tail（后 N 行）、"
                "range（指定行号范围）。默认 head 100 行。"
                "用于查看 BashTool 落盘的命令输出（output_file 字段）、"
                "源代码片段、日志文件等。最大返回 100KB。"
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
                description="读前 N 行（默认 100）。与 tail / range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="tail",
                type="number",
                description="读后 N 行。与 head / range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="start_line",
                type="number",
                description="range 模式起始行号（1-based，含）。",
                required=False,
            ),
            ToolParameter(
                name="end_line",
                type="number",
                description="range 模式结束行号（1-based，含）。",
                required=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        path = parameters.get("path")
        if not path or not isinstance(path, str):
            return False
        for k in ("head", "tail", "start_line", "end_line"):
            v = parameters.get(k)
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps({"error": "参数验证失败"}, ensure_ascii=False)

        p = Path(parameters["path"]).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
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

        # 互斥校验
        modes_set = sum(1 for x in (head, tail, (start or end)) if x)
        if modes_set > 1:
            return json.dumps(
                {"error": "head / tail / range 互斥，只能选一种模式"},
                ensure_ascii=False,
            )

        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return json.dumps(
                {"error": f"读取失败: {e}", "content": ""},
                ensure_ascii=False,
            )

        lines = text.splitlines()
        total_lines = len(lines)

        if tail:
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

        content = "\n".join(selected)
        truncated = False
        if len(content.encode("utf-8")) > MAX_OUTPUT_BYTES:
            # 按字符近似切到 100KB
            ratio = MAX_OUTPUT_BYTES / len(content.encode("utf-8"))
            keep = int(len(content) * ratio)
            content = content[:keep] + "\n... [超过 100KB 已截断] ..."
            truncated = True

        return json.dumps(
            {
                "path": str(p),
                "mode": mode,
                "total_lines": total_lines,
                "returned_lines": len(selected),
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
