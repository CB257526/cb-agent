"""文件读取工具（流式分页）。

模式：
- head：按行前向迭代，读够 N 行或输出预算即停止
- range：流式跳过到 start_line，再读取目标行
- tail：有界 deque 只保留最后 N 行，不把全文行列表驻留内存
- char_range：增量 UTF-8 解码按字符跳过/截取，不整文件读入
- byte_range：按字节游标读取 artifact（游标更稳定）

不为了 total_lines / total_chars 二次全文件扫描；廉价拿不到时返回 null，
同时提供 file_size_bytes。所有模式返回 has_more 与 continuation cursor。

读取成功后向 ReadStateRegistry 登记 (path, mtime)，供 FileWrite/FileEdit 做
staleness check。
"""

from __future__ import annotations

import codecs
import json
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from tools.tool import Tool, ToolParameter
from tools.tools.file_state import get_read_state_registry

MAX_OUTPUT_BYTES = 32 * 1024  # 单次返回内容硬上限
_READ_CHUNK_BYTES = 64 * 1024


class FileReadTool(Tool):
    def __init__(self):
        super().__init__(
            name="file_read",
            description=(
                "读取文本文件。支持五种模式：head（前 N 行）、tail（后 N 行）、"
                "range（指定行号范围）、char_range（指定字符范围）、"
                "byte_range（指定字节范围）。默认 head 100 行。"
                "用于查看 BashTool 落盘的命令输出（output_file 字段）、"
                "源代码片段、日志文件等。最大返回 32KB。"
                "在了解文件内容的情况下，请尽量使用 head/tail/range/"
                "start_char/end_char/start_byte/end_byte 限制返回范围。"
                "响应含 has_more 与 next_start_line/next_start_char/next_start_byte "
                "便于续读；total_lines/total_chars 在未全量扫描时可能为 null。"
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
                description="读前 N 行（默认 100）。与 tail / range / char_range / byte_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="tail",
                type="number",
                description="读后 N 行。与 head / range / char_range / byte_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="start_line",
                type="number",
                description="range 模式起始行号（1-based，含）。与 char_range / byte_range 互斥。",
                required=False,
            ),
            ToolParameter(
                name="end_line",
                type="number",
                description="range 模式结束行号（1-based，含）。与 char_range / byte_range 互斥。",
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
            ToolParameter(
                name="start_byte",
                type="number",
                description="byte_range 模式起始字节偏移（0-based，含）。适合 artifact 续读。",
                required=False,
            ),
            ToolParameter(
                name="end_byte",
                type="number",
                description="byte_range 模式结束字节偏移（0-based，不含）。省略则读到预算上限。",
                required=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> Any:
        if not isinstance(parameters, dict):
            return {"error": "参数必须是字典类型"}
        path = parameters.get("path")
        if not path or not isinstance(path, str):
            return {"error": "path 参数必须是有效的字符串"}
        for k in (
            "head", "tail", "start_line", "end_line",
            "start_char", "end_char", "start_byte", "end_byte",
        ):
            v = parameters.get(k)
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                return {"error": f"{k} 参数必须是有效的非负数"}
        return {"error": None}

    def run(self, parameters: Dict[str, Any]) -> str:
        validation_result = self.validate_parameters(parameters)
        if validation_result["error"]:
            return json.dumps({"error": validation_result["error"]}, ensure_ascii=False)

        p = Path(parameters["path"]).expanduser()
        if not p.is_absolute():
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

        head = self._to_int(parameters.get("head"))
        tail = self._to_int(parameters.get("tail"))
        start = self._to_int(parameters.get("start_line"))
        end = self._to_int(parameters.get("end_line"))
        start_char = self._to_int(parameters.get("start_char"))
        end_char = self._to_int(parameters.get("end_char"))
        # byte 允许 0
        start_byte = self._to_int_allow_zero(parameters.get("start_byte"))
        end_byte = self._to_int_allow_zero(parameters.get("end_byte"))

        byte_mode = start_byte is not None or end_byte is not None
        mode_flags = [
            bool(head),
            bool(tail),
            bool(start or end),
            bool(start_char or end_char),
            byte_mode,
        ]
        if sum(1 for flag in mode_flags if flag) > 1:
            return json.dumps(
                {"error": "head / tail / range / char_range / byte_range 互斥，只能选一种模式"},
                ensure_ascii=False,
            )

        try:
            file_size = p.stat().st_size
            mtime_ns = p.stat().st_mtime_ns
        except OSError as e:
            return json.dumps(
                {"error": f"stat 失败: {e}", "content": ""},
                ensure_ascii=False,
            )

        try:
            if byte_mode:
                payload = self._read_byte_range(p, start_byte, end_byte, file_size)
            elif start_char or end_char:
                payload = self._read_char_range(p, start_char, end_char, file_size)
            elif tail:
                payload = self._read_tail(p, tail, file_size)
            elif start or end:
                payload = self._read_line_range(p, start, end, file_size)
            else:
                payload = self._read_head(p, head or 100, file_size)
        except OSError as e:
            return json.dumps(
                {"error": f"读取失败: {e}", "content": str(e)},
                ensure_ascii=False,
            )

        # staleness：读取成功后登记
        get_read_state_registry().mark_read(p)

        # 读取期间文件是否被改写
        try:
            now_mtime = p.stat().st_mtime_ns
            if now_mtime != mtime_ns:
                payload["staleness_warning"] = (
                    "读取期间文件 mtime 发生变化，内容可能不一致；请重新读取。"
                )
        except OSError:
            pass

        payload["path"] = str(p)
        payload["file_size_bytes"] = file_size
        # 兼容字段：未全量扫描时为 null，避免为统计再扫一遍超大文件。
        payload.setdefault("total_lines", None)
        payload.setdefault("total_chars", None)
        return json.dumps(payload, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 流式读取实现
    # ------------------------------------------------------------------

    def _read_head(self, path: Path, n: int, file_size: int) -> Dict[str, Any]:
        lines: List[str] = []
        has_more = False
        next_start_line: Optional[int] = None
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fp:
            for line_no, raw in enumerate(fp, start=1):
                if line_no > n:
                    has_more = True
                    next_start_line = line_no
                    break
                lines.append(self._strip_newline(raw))
                content_so_far = "\n".join(lines)
                if self._utf8_len(content_so_far) > MAX_OUTPUT_BYTES:
                    content, _ = self._truncate_to_output_limit(content_so_far)
                    # 预算截断后从下一行续读；若本行已是最后请求行，再 peek 文件是否还有内容。
                    has_more = True
                    next_start_line = line_no + 1
                    return self._line_payload(
                        mode=f"head-{n}",
                        content=content,
                        returned_lines=len(lines),
                        has_more=True,
                        next_start_line=next_start_line,
                        truncated=True,
                        start_line=1,
                        end_line=line_no,
                    )

        content = "\n".join(lines)
        content, truncated = self._truncate_to_output_limit(content)
        if truncated:
            has_more = True
            next_start_line = (len(lines) + 1) if lines else 1
        return self._line_payload(
            mode=f"head-{n}",
            content=content,
            returned_lines=len(lines),
            has_more=has_more,
            next_start_line=next_start_line if has_more else None,
            truncated=truncated,
            start_line=1 if lines else None,
            end_line=len(lines) if lines else None,
        )

    def _read_line_range(
        self,
        path: Path,
        start: Optional[int],
        end: Optional[int],
        file_size: int,
    ) -> Dict[str, Any]:
        s = max(1, start or 1)
        e = end  # None = 读到文件尾或预算上限
        lines: List[str] = []
        has_more = False
        next_start_line: Optional[int] = None
        last_line_no = 0
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fp:
            for line_no, raw in enumerate(fp, start=1):
                last_line_no = line_no
                if line_no < s:
                    continue
                if e is not None and line_no > e:
                    has_more = True
                    next_start_line = line_no
                    break
                lines.append(self._strip_newline(raw))
                content_so_far = "\n".join(lines)
                if self._utf8_len(content_so_far) > MAX_OUTPUT_BYTES:
                    content, _ = self._truncate_to_output_limit(content_so_far)
                    has_more = True
                    next_start_line = line_no + 1
                    return self._line_payload(
                        mode=f"range-{s}-{e if e is not None else 'eof'}",
                        content=content,
                        returned_lines=len(lines),
                        has_more=True,
                        next_start_line=next_start_line,
                        truncated=True,
                        start_line=s,
                        end_line=line_no,
                    )
            else:
                # 到 EOF
                if e is not None and last_line_no >= e:
                    # 刚好读完请求范围
                    peek_possible = False
                else:
                    peek_possible = False
                has_more = False
                next_start_line = None

        # 若指定了 end 但文件更长：上面 break 已处理；若 end 大于文件长度则 has_more=False
        # 若未指定 end 且读到 EOF：has_more=False
        # 若 break 因为 line_no > e：已设 has_more
        content = "\n".join(lines)
        content, truncated = self._truncate_to_output_limit(content)
        if truncated and not has_more:
            has_more = True
            next_start_line = (s + len(lines)) if lines else s
        mode_end = e if e is not None else (s + len(lines) - 1 if lines else s)
        return self._line_payload(
            mode=f"range-{s}-{mode_end}",
            content=content,
            returned_lines=len(lines),
            has_more=has_more,
            next_start_line=next_start_line,
            truncated=truncated,
            start_line=s if lines else None,
            end_line=(s + len(lines) - 1) if lines else None,
        )

    def _read_tail(self, path: Path, n: int, file_size: int) -> Dict[str, Any]:
        """有界 deque 保留最后 n 行；时间 O(文件) 但内存 O(n)。"""
        buf: Deque[str] = deque(maxlen=max(1, n))
        with path.open("r", encoding="utf-8", errors="replace", newline="") as fp:
            for raw in fp:
                buf.append(self._strip_newline(raw))
        lines = list(buf)
        content = "\n".join(lines)
        content, truncated = self._truncate_to_output_limit(content, keep_tail=True)
        return self._line_payload(
            mode=f"tail-{n}",
            content=content,
            returned_lines=len(lines),
            has_more=False,  # tail 语义是文件尾，无“下一页”
            next_start_line=None,
            truncated=truncated,
            start_line=None,
            end_line=None,
        )

    def _read_char_range(
        self,
        path: Path,
        start_char: Optional[int],
        end_char: Optional[int],
        file_size: int,
    ) -> Dict[str, Any]:
        """按 Unicode 字符流式跳过/截取，不整文件加载。"""
        cs = max(1, start_char or 1)
        ce = end_char  # None = 直到预算
        if ce is not None and ce < cs:
            return {
                "error": "end_char 必须大于或等于 start_char",
                "content": "",
                "mode": f"char_range-{cs}-{ce}",
            }

        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        out_chars: List[str] = []
        char_index = 0  # 已看到的字符数（0-based 之后 +1 为位置）
        has_more = False
        next_start_char: Optional[int] = None
        reached_end = False

        with path.open("rb") as fp:
            while True:
                chunk = fp.read(_READ_CHUNK_BYTES)
                if not chunk:
                    # finalize
                    tail = decoder.decode(b"", final=True)
                    for ch in tail:
                        char_index += 1
                        if char_index < cs:
                            continue
                        if ce is not None and char_index > ce:
                            has_more = True
                            next_start_char = char_index
                            reached_end = True
                            break
                        out_chars.append(ch)
                        if self._utf8_len("".join(out_chars)) > MAX_OUTPUT_BYTES:
                            has_more = True
                            next_start_char = char_index + 1
                            reached_end = True
                            break
                    break
                text = decoder.decode(chunk, final=False)
                for ch in text:
                    char_index += 1
                    if char_index < cs:
                        continue
                    if ce is not None and char_index > ce:
                        has_more = True
                        next_start_char = char_index
                        reached_end = True
                        break
                    out_chars.append(ch)
                    if self._utf8_len("".join(out_chars)) > MAX_OUTPUT_BYTES:
                        has_more = True
                        next_start_char = char_index + 1
                        reached_end = True
                        break
                if reached_end:
                    break

        content = "".join(out_chars)
        content, truncated = self._truncate_to_output_limit(content)
        actual_end = cs + len(out_chars) - 1 if out_chars else cs - 1
        # 循环内若因 ce/预算提前 break 已设置 has_more；此处只补预算二次截断。
        if truncated and not has_more:
            has_more = True
            next_start_char = actual_end + 1
        if not has_more:
            next_start_char = None

        mode_end = ce if ce is not None else max(cs, actual_end)
        return {
            "mode": f"char_range-{cs}-{mode_end}",
            "content": content,
            "returned_lines": len(content.splitlines()) if content else 0,
            "returned_chars": len(content),
            "char_range": [cs, max(cs, actual_end)] if out_chars else [cs, cs - 1],
            "truncated": truncated,
            "has_more": has_more,
            "next_start_line": None,
            "next_start_char": next_start_char if has_more else None,
            "next_start_byte": None,
            "total_lines": None,
            "total_chars": None,
        }

    def _read_byte_range(
        self,
        path: Path,
        start_byte: Optional[int],
        end_byte: Optional[int],
        file_size: int,
    ) -> Dict[str, Any]:
        sb = max(0, start_byte or 0)
        eb = end_byte if end_byte is not None else None
        if eb is not None and eb < sb:
            return {
                "error": "end_byte 必须大于或等于 start_byte",
                "content": "",
                "mode": f"byte_range-{sb}-{eb}",
            }
        # 预算内读取
        max_read = MAX_OUTPUT_BYTES
        if eb is not None:
            want = max(0, eb - sb)
            read_len = min(want, max_read + 1024)  # 略多读一点再截断文本
        else:
            read_len = max_read + 1024

        with path.open("rb") as fp:
            fp.seek(sb)
            data = fp.read(read_len)
            # 探测是否还有更多
            more = False
            if eb is not None:
                more = (sb + len(data)) < min(file_size, eb) or (sb + len(data)) < file_size and (sb + len(data)) < eb
                if (sb + len(data)) >= eb:
                    data = data[: max(0, eb - sb)]
                    more = (sb + len(data)) < file_size
            else:
                peek = fp.read(1)
                more = bool(peek) or (sb + len(data)) < file_size

        content = data.decode("utf-8", errors="replace")
        content, truncated = self._truncate_to_output_limit(content)
        consumed = len(data)
        next_byte = sb + consumed
        if truncated:
            # 截断后 cursor 按实际返回内容的近似字节
            next_byte = sb + len(content.encode("utf-8", errors="replace"))
            more = True
        has_more = more or truncated
        if next_byte >= file_size:
            has_more = False
            next_byte_out = None
        else:
            next_byte_out = next_byte if has_more else None

        return {
            "mode": f"byte_range-{sb}-{eb if eb is not None else sb + consumed}",
            "content": content,
            "returned_lines": len(content.splitlines()) if content else 0,
            "returned_chars": len(content),
            "byte_range": [sb, sb + consumed],
            "truncated": truncated,
            "has_more": has_more,
            "next_start_line": None,
            "next_start_char": None,
            "next_start_byte": next_byte_out,
            "total_lines": None,
            "total_chars": None,
        }

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _line_payload(
        *,
        mode: str,
        content: str,
        returned_lines: int,
        has_more: bool,
        next_start_line: Optional[int],
        truncated: bool,
        start_line: Optional[int],
        end_line: Optional[int],
    ) -> Dict[str, Any]:
        return {
            "mode": mode,
            "content": content,
            "returned_lines": returned_lines,
            "returned_chars": len(content),
            "char_range": None,
            "truncated": truncated,
            "has_more": has_more,
            "next_start_line": next_start_line if has_more else None,
            "next_start_char": None,
            "next_start_byte": None,
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": None,
            "total_chars": None,
        }

    @staticmethod
    def _strip_newline(raw: str) -> str:
        if raw.endswith("\r\n"):
            return raw[:-2]
        if raw.endswith("\n") or raw.endswith("\r"):
            return raw[:-1]
        return raw

    @staticmethod
    def _utf8_len(text: str) -> int:
        return len(text.encode("utf-8", errors="replace"))

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
    def _to_int_allow_zero(v: Any) -> Optional[int]:
        if v is None:
            return None
        try:
            iv = int(v)
            return iv if iv >= 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truncate_to_output_limit(content: str, *, keep_tail: bool = False) -> Tuple[str, bool]:
        """截断 content 到 MAX_OUTPUT_BYTES 字节以内。"""
        encoded = content.encode("utf-8")
        if len(encoded) <= MAX_OUTPUT_BYTES:
            return content, False

        marker = (
            f"\n... [超过 {MAX_OUTPUT_BYTES} 字节已截断，"
            "可用 start_line/end_line 或 start_char/end_char 或 start_byte/end_byte 继续分段读取] ..."
        )
        marker_bytes = marker.encode("utf-8")
        budget = max(0, MAX_OUTPUT_BYTES - len(marker_bytes))

        if keep_tail:
            clipped = encoded[-budget:] if budget else b""
            clipped_text = clipped.decode("utf-8", errors="ignore")
            return marker.lstrip() + "\n" + clipped_text, True

        clipped = encoded[:budget] if budget else b""
        clipped_text = clipped.decode("utf-8", errors="ignore")
        return clipped_text + marker, True
