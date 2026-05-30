"""命令输出缓冲与落盘策略

参考 Claude Code BashTool 输出处理：

- 内存阈值：stdout 100KB / stderr 20KB（中文字符按字符数算，不按字节）
- 落盘阈值：> 1MB 时把**完整原始输出**写到 ./.cbagent/bash_outputs/<task_id>.log，
  返回 JSON 带 output_file 路径让模型用 FileReadTool 自取
- 64MB 上限：超过即丢弃（参考实现是杀进程，cb-agent 同步 communicate 拿不到流，
  这里在收到完整 stdout 后才判断，超阈值丢弃后续部分）

OutputBuffer 不直接管 Popen，由 BashTool.run 在 communicate 完后把字符串 feed 进来。
仅做截断 + 落盘 + 元数据，输出格式无副作用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


MAX_STDOUT_CHARS = 100_000
MAX_STDERR_CHARS = 20_000
PERSIST_THRESHOLD_BYTES = 1 * 1024 * 1024     # 1MB → 落盘
HARD_CAP_BYTES = 64 * 1024 * 1024              # 64MB → 截断丢弃


@dataclass
class ProcessedOutput:
    stdout: str                # 已截断（≤ MAX_STDOUT_CHARS）
    stderr: str                # 已截断
    output_truncated: bool     # 内存截断是否触发
    output_file: Optional[str] # 落盘路径（绝对路径），未触发为 None
    raw_size_bytes: int        # 原始 stdout 字节数（落盘判断用）


def process_output(
    stdout: str,
    stderr: str,
    output_dir: Path,
    task_id: str,
) -> ProcessedOutput:
    """对一次命令的 stdout/stderr 做内存截断 + 视情况落盘。

    Args:
        stdout / stderr: 原始字符串（已 utf-8 解码）
        output_dir: 落盘目录，调用方负责确保存在
        task_id: 落盘文件名前缀，建议用 BashTool 生成的 uuid 短串
    """
    raw_stdout = stdout or ""
    raw_stderr = stderr or ""
    raw_bytes = len(raw_stdout.encode("utf-8", errors="replace"))

    # 64MB 硬上限：直接丢弃多余字节（按字符近似切，超大输出场景不追求精确）
    if raw_bytes > HARD_CAP_BYTES:
        # 按平均 utf-8 字节比例反推字符数
        ratio = HARD_CAP_BYTES / raw_bytes
        keep_chars = int(len(raw_stdout) * ratio)
        raw_stdout = raw_stdout[:keep_chars] + (
            f"\n\n... [输出超过 {HARD_CAP_BYTES // 1024 // 1024}MB 上限，已丢弃后续] ..."
        )
        raw_bytes = HARD_CAP_BYTES

    # 落盘判断（超过 1MB 就把**完整原始输出**写文件）
    output_file: Optional[str] = None
    if raw_bytes > PERSIST_THRESHOLD_BYTES:
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            log_path = output_dir / f"{task_id}.log"
            log_path.write_text(raw_stdout, encoding="utf-8", errors="replace")
            output_file = str(log_path.resolve())
        except OSError:
            # 落盘失败不致命，模型仍能拿截断版
            output_file = None

    # 内存截断
    stdout_truncated = len(raw_stdout) > MAX_STDOUT_CHARS
    stderr_truncated = len(raw_stderr) > MAX_STDERR_CHARS

    final_stdout = (
        raw_stdout[:MAX_STDOUT_CHARS]
        + f"\n\n... [{len(raw_stdout) - MAX_STDOUT_CHARS} 字符已截断"
        + (f"，完整输出见 {output_file}" if output_file else "")
        + "] ..."
    ) if stdout_truncated else raw_stdout

    final_stderr = (
        raw_stderr[:MAX_STDERR_CHARS]
        + f"\n... [{len(raw_stderr) - MAX_STDERR_CHARS} 字符已截断] ..."
    ) if stderr_truncated else raw_stderr

    return ProcessedOutput(
        stdout=final_stdout,
        stderr=final_stderr,
        output_truncated=stdout_truncated or stderr_truncated,
        output_file=output_file,
        raw_size_bytes=raw_bytes,
    )


def default_output_dir() -> Path:
    """默认落盘根目录：./.cbagent/bash_outputs/。"""
    return Path(os.getcwd()) / ".cbagent" / "bash_outputs"
