"""Executor 层统一工具结果上限 —— 截断 + 持久化 + 防循环。

设计目标：
- 单条 tool result 超过 MAX_SINGLE_RESULT_CHARS 时，完整内容持久化到磁盘，
  消息体替换为 preview（首尾片段）+ 文件路径 + 分段读取引导
- 单轮所有 tool results 总字符超过 MAX_BATCH_RESULT_CHARS 时，从最长的开始
  逐条持久化，直到总量降到预算内
- 防循环：模型用 file_read 读取持久化文件时，不再二次持久化，只做 inline 截断

与现有压缩机制的关系：
- bash_output.py 工具级截断：保持不变，executor cap 是兜底
- _maybe_compress_tool_loop_messages：在 80% 窗口时替换 tool content 为摘要，
  executor cap 在它之前生效（工具返回时就做），两者互不冲突
- legacy local microcompact：按条数替换旧 tool result 为占位，处理跨轮积累的旧消息
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


# ========== 常量 ==========

MAX_SINGLE_RESULT_CHARS = 50_000       # 单条 tool result 字符上限
MAX_BATCH_RESULT_CHARS = 200_000       # 单轮所有 tool results 总字符上限
PREVIEW_HEAD_CHARS = 2000              # preview 保留头部字符数
PREVIEW_TAIL_CHARS = 500               # preview 保留尾部字符数
PERSIST_DIR_NAME = "tool_results"      # 持久化子目录名（在 .cbagent/ 下）

# 用于判断 file_read 是否在读取持久化结果
PERSIST_DIR_MARKER = "tool_results/"


# ========== 防循环判断 ==========


def _is_reading_persisted_result(tool_name: str, result: str) -> bool:
    """判断这次工具调用是否在读取之前持久化的 tool result 文件。

    只检查 file_read 工具且返回 JSON 中的 path 包含 tool_results/ 标记。
    """
    if tool_name != "file_read":
        return False
    try:
        data = json.loads(result)
        path = str(data.get("path", ""))
        return PERSIST_DIR_MARKER in path.replace("\\", "/")
    except (json.JSONDecodeError, TypeError, AttributeError):
        return False


def _extract_existing_persist_path(result: str) -> Optional[str]:
    """检查工具返回的 JSON 中是否已包含持久化文件路径。

    部分工具（如 bash）在输出超限时自行将全文存盘，并在返回的 JSON 里
    包含 output_file 字段。此时 executor 层无需重复持久化，直接复用该路径。

    支持检测的字段：output_file（bash_output.py 使用）。
    """
    try:
        data = json.loads(result)
        if not isinstance(data, dict):
            return None
        # bash_output.py 的落盘路径字段
        output_file = data.get("output_file")
        if output_file and isinstance(output_file, str):
            return output_file
        return None
    except (json.JSONDecodeError, TypeError, AttributeError):
        return None


# ========== 持久化 ==========


def _persist_full_result(
    result: str,
    call_id: str,
    persist_dir: Path,
) -> Optional[str]:
    """把完整 tool result 写到磁盘。返回持久化文件的路径字符串，失败返回 None。"""
    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
        # call_id 可能含不合法文件名字符，做一次清理
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in call_id)
        file_path = persist_dir / f"{safe_id}.txt"
        file_path.write_text(result, encoding="utf-8", errors="replace")
        return str(file_path)
    except OSError as e:
        logger.warning("tool result 持久化失败: call_id=%s err=%s", call_id, e)
        return None


def _build_truncated_payload(
    result: str,
    tool_name: str,
    persisted_path: str,
) -> str:
    """构建截断后的 JSON 替换内容：preview + 元信息 + 分段读取引导。"""
    total_chars = len(result)
    total_lines = result.count("\n") + 1
    head = result[:PREVIEW_HEAD_CHARS]
    tail = result[-PREVIEW_TAIL_CHARS:] if total_chars > PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS else ""

    payload = {
        "truncated": True,
        "tool_name": tool_name,
        "total_chars": total_chars,
        "total_lines": total_lines,
        "preview_head": head,
        "preview_tail": tail,
        "persisted_path": persisted_path,
        "hint": (
            f"完整内容已持久化（{total_chars} 字符 / {total_lines} 行）。"
            f"请用 file_read(path=\"{persisted_path}\", start_line=X, end_line=Y) "
            "按行分段读取；如果是超长单行，用 start_char/end_char 按字符分段读取。"
            "不要一次性全量读取。"
        ),
    }
    return json.dumps(payload, ensure_ascii=False)


# ========== 核心截断函数 ==========


def cap_single_result(
    result: str,
    call_id: str,
    tool_name: str,
    persist_dir: Path,
) -> Tuple[str, bool]:
    """对单条 tool result 做上限检查。

    返回 (可能被截断的 result, 是否触发了持久化)。

    防循环：如果检测到是 file_read 读取持久化文件的结果，只做 inline 硬截断，
    不再二次持久化。
    """
    if not isinstance(result, str):
        result = str(result)

    # 未超限，直接返回
    if len(result) <= MAX_SINGLE_RESULT_CHARS:
        return result, False

    # 防循环：读取持久化文件的 file_read 结果，只做 inline 截断
    if _is_reading_persisted_result(tool_name, result):
        truncated = result[:MAX_SINGLE_RESULT_CHARS] + (
            "\n...[已截断，请用 start_line/end_line 或 start_char/end_char 分段读取]"
        )
        logger.info(
            "cap_single_result: 读取持久化文件的 file_read 结果已 inline 截断"
            " (tool=%s, call_id=%s, original=%d chars)",
            tool_name, call_id, len(result),
        )
        return truncated, False

    # 检查工具是否已经自行持久化了完整输出（如 bash 的 output_file 字段）
    existing_path = _extract_existing_persist_path(result)
    if existing_path:
        # 工具已持久化，不再重复存盘，直接用已有路径构建 payload
        payload = _build_truncated_payload(result, tool_name, existing_path)
        logger.info(
            "cap_single_result: 工具已自行持久化，复用路径"
            " (tool=%s, call_id=%s, original=%d chars, path=%s)",
            tool_name, call_id, len(result), existing_path,
        )
        return payload, False

    # 正常超限：持久化 + 替换为 preview
    persisted_path = _persist_full_result(result, call_id, persist_dir)
    if persisted_path is None:
        # 持久化失败，退化为 inline 截断
        truncated = result[:MAX_SINGLE_RESULT_CHARS] + (
            f"\n...[超过 {MAX_SINGLE_RESULT_CHARS} 字符上限已截断，持久化失败]"
        )
        return truncated, False

    payload = _build_truncated_payload(result, tool_name, persisted_path)
    logger.info(
        "cap_single_result: 持久化完成 (tool=%s, call_id=%s, original=%d chars, path=%s)",
        tool_name, call_id, len(result), persisted_path,
    )
    return payload, True


def cap_batch_results(
    results: "List[_ToolCallResultLike]",
    persist_dir: Path,
) -> None:
    """对一批 tool results 做总量上限检查。原地修改 results 中的 result 字段。

    逻辑：
    1. 先计算已被 cap_single_result 处理后的总字符数
    2. 如果总量 <= MAX_BATCH_RESULT_CHARS，什么都不做
    3. 超限时，按结果长度从大到小排序，依次将最长的持久化截断，
       直到总量降到预算内

    results 中每个元素需要有 .result / .call_id / .name 属性（ToolCallResult 协议）。
    """
    total = sum(len(r.result) if isinstance(r.result, str) else len(str(r.result)) for r in results)
    if total <= MAX_BATCH_RESULT_CHARS:
        return

    # 按当前 result 长度降序排列的索引
    indexed = sorted(
        range(len(results)),
        key=lambda i: len(results[i].result) if isinstance(results[i].result, str) else len(str(results[i].result)),
        reverse=True,
    )

    for idx in indexed:
        if total <= MAX_BATCH_RESULT_CHARS:
            break
        r = results[idx]
        current = r.result if isinstance(r.result, str) else str(r.result)

        # 已经很短的跳过（比如已经被 cap_single_result 截断过）
        if len(current) <= PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS + 200:
            continue

        # 已经是截断 payload 的跳过
        if current.startswith('{"truncated": true') or current.startswith('{"truncated":true'):
            continue

        # 持久化并替换
        persisted_path = _persist_full_result(current, r.call_id, persist_dir)
        if persisted_path is None:
            # 持久化失败，做 inline 硬截断
            new_result = current[:MAX_SINGLE_RESULT_CHARS] + (
                f"\n...[批量截断：总量超 {MAX_BATCH_RESULT_CHARS} 字符上限]"
            )
        else:
            new_result = _build_truncated_payload(current, r.name, persisted_path)
            logger.info(
                "cap_batch_results: 批量持久化 (tool=%s, call_id=%s, "
                "original=%d chars, path=%s)",
                r.name, r.call_id, len(current), persisted_path,
            )

        old_len = len(current)
        r.result = new_result
        total -= (old_len - len(new_result))


def default_persist_dir() -> Path:
    """默认持久化根目录：./.cbagent/tool_results/。"""
    return Path(os.getcwd()) / ".cbagent" / PERSIST_DIR_NAME


__all__ = [
    "MAX_SINGLE_RESULT_CHARS",
    "MAX_BATCH_RESULT_CHARS",
    "PREVIEW_HEAD_CHARS",
    "PREVIEW_TAIL_CHARS",
    "cap_single_result",
    "cap_batch_results",
    "default_persist_dir",
]
