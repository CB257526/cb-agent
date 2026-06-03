"""消息日志：将每次 LLM 调用的完整 messages 列表写入结构化日志文件。

格式：可读文本，每条消息带时间戳、序号、角色、内容。
内容超过 4000 字符自动截断，末尾标注原始长度。

使用方式：
    logger = MessageLogger("/path/to/messages.log")
    logger.log(messages, label="初始上下文")
    logger.log(messages, label="第 2 轮 think 前")
    logger.close()
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


# 每条消息内容最多显示的字符数
CONTENT_MAX_CHARS = 4000


def _truncate(text: str, max_chars: int = CONTENT_MAX_CHARS) -> str:
    """截断文本，末尾标注原始长度。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n…[截断，共 {len(text)} 字符]"


def _flat_content(content: Any) -> str:
    """将 OpenAI message content 转为可读字符串。

    处理三种形态：
    - 纯字符串
    - 多模态数组（[{type: text, text: ...}, {type: image_url, ...}]）
    - None（assistant 发出 tool_calls 时 content 可能为空）
    """
    if content is None:
        return "(无文本内容)"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            t = item.get("type", "")
            if t == "text":
                parts.append(str(item.get("text", "")))
            elif t == "image_url":
                url = (item.get("image_url") or {}).get("url", "")[:100]
                parts.append(f"[图片: {url}]")
            elif t == "audio_url":
                url = (item.get("audio_url") or {}).get("url", "")[:100]
                parts.append(f"[音频: {url}]")
            else:
                parts.append(f"[{t}]")
        return " ".join(p for p in parts if p).strip() or "(空内容)"
    return str(content)


def _tool_calls_text(tool_calls: List[Dict[str, Any]]) -> str:
    """渲染 assistant 消息里的 tool_calls 字段。"""
    lines: List[str] = []
    for tc in tool_calls:
        fid = tc.get("id", "?")
        func = tc.get("function", {})
        name = func.get("name", "?")
        args_raw = func.get("arguments", "{}")
        if isinstance(args_raw, str):
            try:
                args_obj = json.loads(args_raw)
                args_str = json.dumps(args_obj, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError):
                args_str = args_raw
        else:
            args_str = str(args_raw)
        args_str = _truncate(args_str, 1500)
        lines.append(f"  ── tool_call id={fid} ──\n  {name}({args_str})")
    return "\n".join(lines)


class MessageLogger:
    """将会话消息写入专用日志文件。

    线程安全：write() 依赖 Python 文件对象的内部缓冲，多线程并发调用
    时可能产生交错行；此项目消息日志只在单线程的 asyncio 主循环中调用，
    因此不需要加锁。
    """

    def __init__(self, file_path: Path | str):
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(str(self._path), "a", encoding="utf-8")
        self._write_header()

    def _write_header(self):
        self._file.write(
            f"cb-agent 消息日志\n"
            f"启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'='*70}\n"
        )
        self._file.flush()

    def log(
        self,
        messages: List[Dict[str, Any]],
        label: str = "",
    ) -> None:
        """将消息列表写入日志文件。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        count = len(messages)
        total_chars = sum(
            len(str(_flat_content(m.get("content")))) for m in messages
        )

        lines: List[str] = []
        lines.append("")
        lines.append("─" * 70)
        header = f"[{now}] {label}"
        if label:
            header += "  "
        header += f"messages={count}  chars≈{total_chars}"
        lines.append(header)
        lines.append("─" * 70)

        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content")

            # 构建消息头
            tag = f"[{i}] {role.upper()}"
            if role == "tool":
                tag += f" | name={msg.get('name', '?')}"
                tag += f" | call_id={msg.get('tool_call_id', '?')}"
            elif role == "assistant":
                tcs = msg.get("tool_calls") or []
                if tcs:
                    tc_names = [
                        tc.get("function", {}).get("name", "?") for tc in tcs
                    ]
                    tag += f" | tool_calls={tc_names}"
            elif role == "system":
                # 显示系统提示的前 80 字符作为预览
                preview = _flat_content(content)[:80].replace("\n", " ")
                tag += f" | preview=\"{preview}...\"" if len(str(content or "")) > 80 else f" | \"{preview}\""

            lines.append("")
            lines.append(tag)

            # 输出文本内容
            text = _flat_content(content)
            if text:
                lines.append(_truncate(text))

            # assistant 的 tool_calls 详情
            if role == "assistant":
                tcs = msg.get("tool_calls")
                if tcs:
                    lines.append(_tool_calls_text(tcs))

        lines.append("")
        self._file.write("\n".join(lines))
        self._file.flush()

    def close(self) -> None:
        """关闭日志文件。"""
        try:
            self._file.write(f"\n{'='*70}\n")
            self._file.write(f"结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._file.flush()
            self._file.close()
        except Exception:
            pass
