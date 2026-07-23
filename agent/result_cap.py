"""Executor 层统一工具结果上限 —— 截断 + 持久化 + 防循环。

设计目标：
- 单条 tool result 超过 MAX_SINGLE_RESULT_TOKENS 或字节兜底上限时，完整内容持久化到磁盘，
  消息体替换为 preview（首尾片段）+ 文件路径 + 分段读取引导
- 单轮所有 tool results 超过批量 token/字节预算时，从最大的开始
  逐条持久化，直到总量降到预算内
- file_read 已有原始文件和分页参数，超限时只缩短 content，不重复持久化

与现有压缩机制的关系：
- bash_output.py 工具级截断：保持不变，executor cap 是兜底
- 工具循环保持追加式，不再改写已经进入请求历史的旧 tool result
- 上下文达到阈值时由 preflight、post-turn 或手动 compact 统一释放空间
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from context.budget.tokens import count_tokens

logger = logging.getLogger(__name__)


# ========== 常量 ==========

MAX_SINGLE_RESULT_TOKENS = 10_000       # 对齐 Codex：单条模型可见工具结果最多约 10K tokens
MAX_SINGLE_RESULT_BYTES = 40_000        # 按 4 bytes/token 设置无需分词即可执行的硬兜底
MAX_BATCH_RESULT_TOKENS = 40_000        # 保留原有四条单条结果的批量预算比例
MAX_BATCH_RESULT_BYTES = 160_000        # 批量字节硬兜底，与 token 预算保持相同比例
PREVIEW_HEAD_CHARS = 2000               # preview 保留头部字符数
PREVIEW_TAIL_CHARS = 500                # preview 保留尾部字符数
PERSIST_DIR_NAME = "tool_results"       # 持久化子目录名（在 .cbagent/ 下）


# ========== 防循环判断 ==========


def _result_size(result: str) -> Tuple[int, int]:
    """返回工具结果的近似 token 数和 UTF-8 字节数。"""
    return count_tokens(result), len(result.encode("utf-8", errors="replace"))


def _within_limit(result: str, *, max_tokens: int, max_bytes: int) -> bool:
    """判断文本是否同时位于 token 预算和字节硬上限以内。"""
    tokens, size_bytes = _result_size(result)
    return tokens <= max_tokens and size_bytes <= max_bytes


def _truncate_inline(
    result: str,
    notice: str,
    *,
    max_tokens: int = MAX_SINGLE_RESULT_TOKENS,
    max_bytes: int = MAX_SINGLE_RESULT_BYTES,
) -> str:
    """按 token 与字节双重预算保留文本头部，并追加明确的截断提示。"""
    if _within_limit(result, max_tokens=max_tokens, max_bytes=max_bytes):
        return result

    # 二分查找可保留的最大字符前缀。每次都对最终字符串计量，确保提示文本本身
    # 也计入模型可见预算，而不是截断后又被元数据顶回上限之外。
    low, high = 0, len(result)
    best = notice if _within_limit(notice, max_tokens=max_tokens, max_bytes=max_bytes) else ""
    while low <= high:
        middle = (low + high) // 2
        candidate = result[:middle] + notice
        if _within_limit(candidate, max_tokens=max_tokens, max_bytes=max_bytes):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _truncate_file_read_payload(
    result: str,
    *,
    max_tokens: int = MAX_SINGLE_RESULT_TOKENS,
    max_bytes: int = MAX_SINGLE_RESULT_BYTES,
) -> str:
    """缩短 file_read 的 content，同时保留路径、范围和分页元数据。

    file_read 的原始文件已经存在，重复写入 tool_results 只会制造副本和读取循环。
    因此这里对 JSON 的 content 字段做预算内截断，并引导模型继续按范围读取。
    """
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        return _truncate_inline(
            result,
            "\n...[达到统一工具结果上限，已截断；请缩小 file_read 读取范围]",
            max_tokens=max_tokens,
            max_bytes=max_bytes,
        )

    original_content = data["content"]
    keep_tail = str(data.get("mode", "")).startswith("tail-")
    notice = (
        "\n... [达到统一工具结果上限，已截断；"
        "请用 start_line/end_line 或 start_char/end_char 继续分段读取] ..."
    )
    previous_hint = str(data.get("hint", "")).strip()
    data["truncated"] = True
    data["result_cap_truncated"] = True
    data["hint"] = " ".join(filter(None, (
        previous_hint,
        "当前结果超过 10K token 上限，请缩小 file_read 读取范围后继续。",
    )))

    def _serialize(keep_chars: int) -> str:
        if keep_tail:
            clipped = notice.lstrip() + "\n" + original_content[-keep_chars:] if keep_chars else notice.lstrip()
        else:
            clipped = original_content[:keep_chars] + notice
        data["content"] = clipped
        data["returned_chars"] = len(clipped)
        return json.dumps(data, ensure_ascii=False)

    # 对完整 JSON 做二分，而不是只计算 content；path、范围和 hint 都必须计入预算。
    low, high = 0, len(original_content)
    best = _serialize(0)
    if not _within_limit(best, max_tokens=max_tokens, max_bytes=max_bytes):
        return _truncate_inline(
            result,
            "\n...[file_read 元数据超过统一工具结果上限，已截断]",
            max_tokens=max_tokens,
            max_bytes=max_bytes,
        )
    while low <= high:
        middle = (low + high) // 2
        candidate = _serialize(middle)
        if _within_limit(candidate, max_tokens=max_tokens, max_bytes=max_bytes):
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


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
        # bash_output：优先 stdout_file，其次兼容 output_file。
        for key in ("stdout_file", "output_file"):
            value = data.get(key)
            if value and isinstance(value, str):
                return value
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
    total_tokens = count_tokens(result)
    total_bytes = len(result.encode("utf-8", errors="replace"))
    total_lines = result.count("\n") + 1
    head = result[:PREVIEW_HEAD_CHARS]
    tail = result[-PREVIEW_TAIL_CHARS:] if total_chars > PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS else ""

    payload = {
        "truncated": True,
        "tool_name": tool_name,
        "total_chars": total_chars,
        "total_tokens": total_tokens,
        "total_bytes": total_bytes,
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

    file_read 只缩短原 JSON 的 content 字段，不重复持久化原文件。
    """
    if not isinstance(result, str):
        result = str(result)

    # token 与字节都未超限时直接返回，不改变进入历史的稳定内容。
    if _within_limit(
        result,
        max_tokens=MAX_SINGLE_RESULT_TOKENS,
        max_bytes=MAX_SINGLE_RESULT_BYTES,
    ):
        return result, False

    # file_read 已经具备路径和分页能力，任何来源的读取结果都不重复落盘。
    if tool_name == "file_read":
        truncated = _truncate_file_read_payload(result)
        logger.info(
            "cap_single_result: file_read 结果已按 token 预算内联截断"
            " (call_id=%s, original_tokens=%d)",
            call_id, count_tokens(result),
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
        # 持久化失败时仍必须遵守模型可见预算，退化为 token/字节双限内联截断。
        truncated = _truncate_inline(
            result,
            f"\n...[超过 {MAX_SINGLE_RESULT_TOKENS} token 上限已截断，持久化失败]",
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
    1. 先计算已被 cap_single_result 处理后的总 token 数和字节数
    2. 如果两项都在批量预算内，什么都不做
    3. 超限时，按 token 数从大到小排序，依次缩短最大的结果，
       直到总量降到预算内

    results 中每个元素需要有 .result / .call_id / .name 属性（ToolCallResult 协议）。
    """
    rendered = [r.result if isinstance(r.result, str) else str(r.result) for r in results]
    total_tokens = sum(count_tokens(item) for item in rendered)
    total_bytes = sum(len(item.encode("utf-8", errors="replace")) for item in rendered)
    if total_tokens <= MAX_BATCH_RESULT_TOKENS and total_bytes <= MAX_BATCH_RESULT_BYTES:
        return

    # 按当前 result token 数降序处理，优先缩短最消耗上下文的结果。
    indexed = sorted(
        range(len(results)),
        key=lambda i: count_tokens(rendered[i]),
        reverse=True,
    )

    for idx in indexed:
        if total_tokens <= MAX_BATCH_RESULT_TOKENS and total_bytes <= MAX_BATCH_RESULT_BYTES:
            break
        r = results[idx]
        current = r.result if isinstance(r.result, str) else str(r.result)

        # 已经很短的跳过（比如已经被 cap_single_result 截断过）
        if len(current) <= PREVIEW_HEAD_CHARS + PREVIEW_TAIL_CHARS + 200:
            continue

        # 已经是截断 payload 的跳过
        if current.startswith('{"truncated": true') or current.startswith('{"truncated":true'):
            continue

        # file_read 不复制原文件；批量压力下把它进一步缩到单条预算的四分之一。
        if r.name == "file_read":
            new_result = _truncate_file_read_payload(
                current,
                max_tokens=MAX_SINGLE_RESULT_TOKENS // 4,
                max_bytes=MAX_SINGLE_RESULT_BYTES // 4,
            )
        else:
            # 其它工具维持现有语义：全文落盘，模型只接收稳定的头尾 preview。
            persisted_path = _persist_full_result(current, r.call_id, persist_dir)
            if persisted_path is None:
                new_result = _truncate_inline(
                    current,
                    f"\n...[批量结果超过 {MAX_BATCH_RESULT_TOKENS} token 上限，已截断]",
                    max_tokens=MAX_SINGLE_RESULT_TOKENS // 4,
                    max_bytes=MAX_SINGLE_RESULT_BYTES // 4,
                )
            else:
                new_result = _build_truncated_payload(current, r.name, persisted_path)
                logger.info(
                    "cap_batch_results: 批量持久化 (tool=%s, call_id=%s, "
                    "original_tokens=%d, path=%s)",
                    r.name, r.call_id, count_tokens(current), persisted_path,
                )

        old_tokens, old_bytes = _result_size(current)
        new_tokens, new_bytes = _result_size(new_result)
        r.result = new_result
        total_tokens -= old_tokens - new_tokens
        total_bytes -= old_bytes - new_bytes


def default_persist_dir() -> Path:
    """默认持久化根目录：./.cbagent/tool_results/。"""
    return Path(os.getcwd()) / ".cbagent" / PERSIST_DIR_NAME


__all__ = [
    "MAX_SINGLE_RESULT_TOKENS",
    "MAX_SINGLE_RESULT_BYTES",
    "MAX_BATCH_RESULT_TOKENS",
    "MAX_BATCH_RESULT_BYTES",
    "PREVIEW_HEAD_CHARS",
    "PREVIEW_TAIL_CHARS",
    "cap_single_result",
    "cap_batch_results",
    "default_persist_dir",
]
