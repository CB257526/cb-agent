"""命令输出缓冲与落盘策略。

参考 Claude Code BashTool 输出处理，并遵守「模型可见截断前先落盘完整原文」：

- 内存预览阈值：stdout 100K 字符 / stderr 20K 字符
- 只要触发模型可见截断，就分别落盘完整 stdout / stderr（不再要求 >1MB 才写文件）
- `stdout_file` / `stderr_file` 为各自完整原文；`output_file` 是 stdout 的兼容别名
- 64MB 硬上限：超出部分无法保证完整保存，标记 hard_limit_exceeded 并写明错误
- 持久化失败：返回明确 persist_error，不得伪装成可恢复的截断成功

后续阶段会把 Popen.communicate 全量收集改为边读边写 spool；本模块接口保持兼容。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


MAX_STDOUT_CHARS = 100_000
MAX_STDERR_CHARS = 20_000
# 兼容旧常量名：超过该字节数时历史上会强制落盘；现已改为“截断即落盘”。
PERSIST_THRESHOLD_BYTES = 1 * 1024 * 1024
HARD_CAP_BYTES = 64 * 1024 * 1024


@dataclass
class ProcessedOutput:
    stdout: str
    stderr: str
    output_truncated: bool
    output_file: Optional[str]  # stdout 兼容别名
    stdout_file: Optional[str]
    stderr_file: Optional[str]
    raw_size_bytes: int
    stdout_chars: int = 0
    stderr_chars: int = 0
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_lines: int = 0
    stderr_lines: int = 0
    hard_limit_exceeded: bool = False
    persist_error: Optional[str] = None


def _count_lines(text: str) -> int:
    if not text:
        return 0
    # 空串 0 行；仅换行也按 splitlines 语义计数。
    return len(text.splitlines())


def _write_text(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")
    return str(path.resolve())


def _preview_with_meta(
    *,
    stream_name: str,
    raw: str,
    keep_chars: int,
    file_path: Optional[str],
    hard_limit_exceeded: bool,
) -> str:
    total_chars = len(raw)
    total_bytes = len(raw.encode("utf-8", errors="replace"))
    total_lines = _count_lines(raw)
    head = raw[:keep_chars]
    omitted = max(0, total_chars - keep_chars)
    lines = [
        head.rstrip("\n"),
        "",
        f"... [{stream_name} 已截断: 保留前 {keep_chars} 字符，省略 {omitted} 字符] ...",
        f"stats: chars={total_chars} bytes={total_bytes} lines={total_lines}",
    ]
    if file_path:
        lines.append(f"full_file: {file_path}")
        lines.append(
            f"续读示例: file_read(path={file_path!r}, head=100) "
            f"或 file_read(path={file_path!r}, start_line=1, end_line=200)"
        )
    else:
        lines.append("full_file: (未落盘)")
    if hard_limit_exceeded:
        lines.append(
            f"hard_limit: 输出超过 {HARD_CAP_BYTES // 1024 // 1024}MB 上限，"
            "已保存的文件可能不完整，不可当作全文。"
        )
    return "\n".join(lines)


def process_output(
    stdout: str,
    stderr: str,
    output_dir: Path,
    task_id: str,
) -> ProcessedOutput:
    """对一次命令的 stdout/stderr 做预览截断 + 必要时落盘完整原文。

    Args:
        stdout / stderr: 原始字符串（已 utf-8 解码）
        output_dir: 落盘目录，调用方负责确保存在
        task_id: 落盘文件名前缀，建议用 BashTool 生成的 uuid 短串
    """
    raw_stdout = stdout or ""
    raw_stderr = stderr or ""

    stdout_chars = len(raw_stdout)
    stderr_chars = len(raw_stderr)
    stdout_bytes = len(raw_stdout.encode("utf-8", errors="replace"))
    stderr_bytes = len(raw_stderr.encode("utf-8", errors="replace"))
    stdout_lines = _count_lines(raw_stdout)
    stderr_lines = _count_lines(raw_stderr)

    hard_limit_exceeded = stdout_bytes > HARD_CAP_BYTES
    persist_error: Optional[str] = None

    # 硬上限：落盘时只写 HARD_CAP 内内容，并明确标记不完整。
    stdout_to_persist = raw_stdout
    if hard_limit_exceeded:
        ratio = HARD_CAP_BYTES / max(1, stdout_bytes)
        keep_chars = max(1, int(stdout_chars * ratio))
        stdout_to_persist = raw_stdout[:keep_chars] + (
            f"\n\n... [输出超过 {HARD_CAP_BYTES // 1024 // 1024}MB 上限，"
            "后续未写入 artifact；不可当作完整输出] ..."
        )

    stdout_truncated = stdout_chars > MAX_STDOUT_CHARS or hard_limit_exceeded
    stderr_truncated = stderr_chars > MAX_STDERR_CHARS

    stdout_file: Optional[str] = None
    stderr_file: Optional[str] = None

    # 模型可见截断 ⇒ 必须先尝试落盘完整（或硬上限内）原文。
    if stdout_truncated:
        try:
            stdout_file = _write_text(
                output_dir / f"{task_id}.stdout.log",
                stdout_to_persist,
            )
        except OSError as error:
            persist_error = f"stdout 落盘失败: {error}"
            stdout_file = None

    if stderr_truncated:
        try:
            stderr_file = _write_text(
                output_dir / f"{task_id}.stderr.log",
                raw_stderr,
            )
        except OSError as error:
            err_msg = f"stderr 落盘失败: {error}"
            persist_error = f"{persist_error}; {err_msg}" if persist_error else err_msg
            stderr_file = None

    # 兼容旧字段：output_file 指向 stdout 完整文件。
    output_file = stdout_file

    if stdout_truncated:
        final_stdout = _preview_with_meta(
            stream_name="stdout",
            raw=raw_stdout if not hard_limit_exceeded else stdout_to_persist,
            keep_chars=MAX_STDOUT_CHARS,
            file_path=stdout_file,
            hard_limit_exceeded=hard_limit_exceeded,
        )
        if persist_error and not stdout_file:
            final_stdout += (
                f"\npersist_error: {persist_error}\n"
                "警告: 完整 stdout 未能落盘，截断内容不可恢复。"
            )
    else:
        final_stdout = raw_stdout

    if stderr_truncated:
        final_stderr = _preview_with_meta(
            stream_name="stderr",
            raw=raw_stderr,
            keep_chars=MAX_STDERR_CHARS,
            file_path=stderr_file,
            hard_limit_exceeded=False,
        )
        if persist_error and not stderr_file:
            final_stderr += (
                f"\npersist_error: {persist_error}\n"
                "警告: 完整 stderr 未能落盘，截断内容不可恢复。"
            )
    else:
        final_stderr = raw_stderr

    return ProcessedOutput(
        stdout=final_stdout,
        stderr=final_stderr,
        output_truncated=stdout_truncated or stderr_truncated,
        output_file=output_file,
        stdout_file=stdout_file,
        stderr_file=stderr_file,
        raw_size_bytes=stdout_bytes,
        stdout_chars=stdout_chars,
        stderr_chars=stderr_chars,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        stdout_lines=stdout_lines,
        stderr_lines=stderr_lines,
        hard_limit_exceeded=hard_limit_exceeded,
        persist_error=persist_error,
    )


def default_output_dir() -> Path:
    """默认落盘根目录：./.cbagent/bash_outputs/。"""
    return Path(os.getcwd()) / ".cbagent" / "bash_outputs"
