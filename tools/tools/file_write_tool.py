"""文件写入工具

参考 Claude Code FileWriteTool 的核心约束做最小搬运：

1. 覆盖已有文件前必须先 file_read 过它，且自那次读取以来 mtime 未变
   —— 防止盲写、防止覆盖 linter / 用户的并发改动
2. 原子写入：先写 tmp 再 os.replace，避免中途失败留下半个文件
3. 目录不存在自动 mkdir -p
4. 拒绝 UNC 路径（\\\\server\\share）—— Windows 上会触发 SMB 认证泄露 NTLM
5. 创建 vs 更新返回不同的 type，附带行数变化用于模型确认结果

提示词层面（prompt 段）的软约束：
- 编辑现有文件优先用 Edit / 后续 file_edit 工具，FileWrite 用于新建或完整重写
- 不主动创建 *.md / README，除非用户明确要求

不处理：
- 备份历史（cb-agent 没有 fileHistoryEnabled 这套）
- LSP / git diff 联动
- 团队共享 secret 检查
"""

from __future__ import annotations

import difflib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from tools.tools.file_state import get_read_state_registry


# 写入大小硬上限：避免模型一次塞 100MB 文本进 tool_result
MAX_WRITE_BYTES = 10 * 1024 * 1024  # 10MB
# diff 最大行数：超出截断，防止 tool_result JSON 膨胀
MAX_DIFF_LINES = 80


class FileWriteTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="file_write",
            description=(
                "写入文件（创建新文件或完整覆盖已有文件）。"
                "覆盖已有文件前必须先用 file_read 读过它（staleness check）。"
                "自动创建父目录。原子写入。"
                "适用于：新建文件、整体重写。"
                "对现有文件做局部修改请用 Edit / file_edit 工具，效率更高。"
                "不要主动创建 *.md / README，除非用户明确要求。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description=(
                    "文件路径，绝对或相对当前 BashSession.cwd。"
                    "Windows UNC 路径（\\\\\\\\server\\\\share\\\\...）会被拒绝。"
                ),
                required=True,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="要写入的完整文件内容（UTF-8 文本）。",
                required=True,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        path = parameters.get("path")
        content = parameters.get("content")
        if not path or not isinstance(path, str):
            return False
        if not isinstance(content, str):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps({"error": "参数验证失败：需要 path(str) + content(str)"},
                              ensure_ascii=False)

        raw_path = parameters["path"]
        content: str = parameters["content"]

        # UNC 路径拒绝（防 NTLM 泄露）
        if raw_path.startswith("\\\\") or raw_path.startswith("//"):
            return json.dumps(
                {"error": "UNC 路径已禁用（Windows 安全约束）", "path": raw_path},
                ensure_ascii=False,
            )

        # 大小上限
        size = len(content.encode("utf-8"))
        if size > MAX_WRITE_BYTES:
            return json.dumps(
                {
                    "error": f"内容超过 {MAX_WRITE_BYTES} 字节上限（实际 {size}）。"
                             f"分块写入或先用 file_edit 局部改。",
                },
                ensure_ascii=False,
            )

        # 路径解析：相对路径走 BashSession.cwd（保持与 file_read 一致）
        p = Path(raw_path).expanduser()
        if not p.is_absolute():
            from tools.tools.bash_session import get_session
            p = Path(get_session().cwd) / p
        try:
            p = p.resolve()
        except OSError as e:
            return json.dumps(
                {"error": f"路径解析失败: {e}"}, ensure_ascii=False,
            )

        # Staleness check：覆盖已有文件前必须 read 过且 mtime 没变
        if p.exists():
            if not p.is_file():
                return json.dumps(
                    {"error": f"路径已存在但不是文件: {p}"},
                    ensure_ascii=False,
                )
            registry = get_read_state_registry()
            recorded_mtime = registry.get_read_mtime(p)
            if recorded_mtime is None:
                return json.dumps(
                    {
                        "error": (
                            f"文件已存在但本会话未用 file_read 读过它。"
                            f"请先 file_read 该文件再 file_write，"
                            f"避免覆盖未知内容。path={p}"
                        ),
                        "needs_read_first": True,
                    },
                    ensure_ascii=False,
                )
            try:
                current_mtime = p.stat().st_mtime_ns
            except OSError as e:
                return json.dumps(
                    {"error": f"读取文件元信息失败: {e}"}, ensure_ascii=False,
                )
            if current_mtime > recorded_mtime:
                return json.dumps(
                    {
                        "error": (
                            "文件自上次 file_read 后被外部修改（mtime 已变）。"
                            "请重新 file_read 再 file_write。"
                        ),
                        "stale": True,
                        "path": str(p),
                    },
                    ensure_ascii=False,
                )
            old_content = self._safe_read(p)
            file_type = "update"
        else:
            old_content = None
            file_type = "create"

        # 自动建目录
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return json.dumps(
                {"error": f"创建父目录失败: {e}"}, ensure_ascii=False,
            )

        # 原子写入：tmp + replace
        try:
            tmp_path = self._atomic_write(p, content)
        except OSError as e:
            return json.dumps(
                {"error": f"写入失败: {e}", "path": str(p)},
                ensure_ascii=False,
            )

        # 写完更新 read 记录，下次同会话直接覆盖不必再读
        get_read_state_registry().mark_read(p)

        # diff 摘要：增/删行数（粗粒度，纯计数不做 patch）
        added, removed = _line_delta(old_content, content)

        # 生成 unified diff，供 TUI 展示变更详情
        diff_text, diff_truncated, diff_total, diff_shown = _generate_unified_diff(
            old_content, content, str(p),
        )

        result: Dict[str, Any] = {
            "ok": True,
            "type": file_type,
            "path": str(p),
            "bytes_written": size,
            "lines_added": added,
            "lines_removed": removed,
            "message": (
                f"已创建 {p}" if file_type == "create"
                else f"已更新 {p}（+{added}/-{removed} 行）"
            ),
        }
        # 仅在 diff 非空时附加（无变更时 unified_diff 返回空列表）
        if diff_text:
            result["diff"] = diff_text
            result["diff_truncated"] = diff_truncated
            result["diff_lines_total"] = diff_total
            result["diff_lines_shown"] = diff_shown

        return json.dumps(result, ensure_ascii=False)

    @staticmethod
    def _safe_read(p: Path) -> Optional[str]:
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @staticmethod
    def _atomic_write(target: Path, content: str) -> Path:
        """先写 tmp 同目录文件再 os.replace 到目标，跨平台原子。"""
        # 同目录是关键：os.replace 跨卷会退化或失败
        fd, tmp_name = tempfile.mkstemp(
            prefix=".cbagent_write_",
            suffix=".tmp",
            dir=str(target.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # 失败时清理 tmp
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        os.replace(tmp_name, str(target))
        return target


def _line_delta(old: Optional[str], new: str) -> tuple[int, int]:
    """粗粒度 +/- 行数。新建文件时 old=None → 全是 added。"""
    if old is None:
        return (len(new.splitlines()), 0)
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    # 不做 LCS，只算总数差异；模型只需感知"改了多少"
    added = max(0, len(new_lines) - len(old_lines))
    removed = max(0, len(old_lines) - len(new_lines))
    # 同等行数但内容不同时，按"全替换"计
    if added == 0 and removed == 0 and old_lines != new_lines:
        diff_count = sum(
            1 for a, b in zip(old_lines, new_lines) if a != b
        )
        return (diff_count, diff_count)
    return (added, removed)


def _generate_unified_diff(
    old: Optional[str],
    new: str,
    file_path: str,
    max_lines: int = MAX_DIFF_LINES,
) -> tuple[str, bool, int, int]:
    """生成 unified diff，供 TUI 展示文件变更。

    关键：OpenTUI / jsdiff 的 parsePatch 要求 hunk 体每行以 ``+``/``-``/`` ``/``\\``
    开头。若用 ``splitlines(keepends=True)`` 且末行无换行，difflib 会把
    ``-old`` 与 ``+new`` 粘成一行 ``-old+new``，触发
    ``Hunk at line N contained invalid line``。

    截断时不能简单 ``lines[:max]``：会切断 hunk 中部，导致
    ``Added/Removed line count did not match``。必须按完整 hunk 截断，
    或对溢出的最后一个 hunk 重写 ``@@`` 计数。

    Returns:
        (diff_text, truncated, total_lines, shown_lines)
    """
    # 不用 keepends：行内容不含换行。再统一补 \n。
    # 注意：部分 Python 版本下 unified_diff(lineterm="\n") 只给 header 加 \n，
    # body（' line'/'-x'/'+y'）可能不带换行；"".join 会粘成 " line1-line2+line2x"。
    if old is None:
        old_lines: list[str] = []
        from_file = "/dev/null"
    else:
        old_lines = old.splitlines()
        from_file = file_path

    new_lines = new.splitlines()
    to_file = file_path

    all_lines = [
        _ensure_diff_line_nl(line)
        for line in difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="\n",
            n=3,
        )
    ]
    total_lines = len(all_lines)
    if total_lines == 0:
        return "", False, 0, 0

    if total_lines <= max_lines:
        return "".join(all_lines), False, total_lines, total_lines

    shown = _truncate_unified_diff_lines(all_lines, max_lines)
    return "".join(shown), True, total_lines, len(shown)


def _ensure_diff_line_nl(line: str) -> str:
    """保证 unified diff 每一行都以 \\n 结尾（防 join 粘行）。"""
    return line if line.endswith("\n") else line + "\n"


def _truncate_unified_diff_lines(lines: list[str], max_lines: int) -> list[str]:
    """按完整 hunk 截断 unified diff；必要时重写最后一个不完整 hunk 的 @@ 计数。"""
    if len(lines) <= max_lines:
        return lines

    header: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].startswith("@@"):
        header.append(lines[i])
        i += 1

    out = header[:]
    if len(out) >= max_lines:
        return out[:max_lines]

    while i < len(lines):
        if not lines[i].startswith("@@"):
            i += 1
            continue

        hunk_header = lines[i]
        i += 1
        body: list[str] = []
        while i < len(lines) and not lines[i].startswith("@@"):
            # 多文件 diff 的下一文件头（本工具单文件，防御性保留）
            if lines[i].startswith("--- ") or lines[i].startswith("diff "):
                break
            body.append(lines[i])
            i += 1

        need = 1 + len(body)
        if len(out) + need <= max_lines:
            out.append(hunk_header)
            out.extend(body)
            continue

        # 装不下完整 hunk：尽量塞 partial body 并重写 @@ 行数
        room = max_lines - len(out) - 1
        if room < 1:
            break
        partial = body[:room]
        rewritten = _rewrite_hunk_header(hunk_header, partial)
        if rewritten is None:
            break
        out.append(rewritten)
        out.extend(partial)
        break

    return out


def _rewrite_hunk_header(hunk_header: str, body: list[str]) -> Optional[str]:
    """根据实际 body 重写 ``@@ -a,b +c,d @@``，使 jsdiff 计数校验通过。"""
    import re

    m = re.match(
        r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@(.*)",
        hunk_header.rstrip("\n"),
    )
    if not m:
        return None

    old_start, new_start, rest = m.group(1), m.group(2), m.group(3) or ""
    old_count = 0
    new_count = 0
    for line in body:
        if not line:
            # 空行在 unified 里极少；跳过避免炸解析
            continue
        op = line[0]
        if op == "+":
            new_count += 1
        elif op == "-":
            old_count += 1
        elif op == " ":
            old_count += 1
            new_count += 1
        elif op == "\\":
            # "\ No newline at end of file" — 不计入 old/new lines
            continue
        else:
            # 非法前缀：无法安全重写
            return None

    # unified 惯例：计数为 0 时 start 仍按原起点写出
    return f"@@ -{old_start},{old_count} +{new_start},{new_count} @@{rest}\n"
