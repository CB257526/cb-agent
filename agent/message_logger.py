"""Level-aware LLM message logging.

The normal runtime log records lifecycle, tool, gateway and error details.
This logger is only for the exact OpenAI-compatible ``messages`` payload that
is sent into the model. It supports two modes:

- ``summary``: roles, sizes, previews and tool-call metadata only.
- ``full``: complete message content and tool-call arguments.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def _flat_content(content: Any) -> str:
    if content is None:
        return "(no text content)"
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if not isinstance(item, dict):
                parts.append(str(item))
                continue
            item_type = item.get("type", "")
            if item_type == "text":
                parts.append(str(item.get("text", "")))
            elif item_type == "image_url":
                url = (item.get("image_url") or {}).get("url", "")[:100]
                parts.append(f"[image: {url}]")
            elif item_type == "audio_url":
                url = (item.get("audio_url") or {}).get("url", "")[:100]
                parts.append(f"[audio: {url}]")
            else:
                parts.append(f"[{item_type}]")
        return " ".join(p for p in parts if p).strip() or "(empty content)"
    return str(content)


def _clip(text: str, limit: int = 240) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _tool_calls_text(tool_calls: List[Dict[str, Any]], *, full: bool) -> str:
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
        if not full:
            args_str = _clip(args_str)
        lines.append(f"  -- tool_call id={fid} --\n  {name}({args_str})")
    return "\n".join(lines)


class MessageLogger:
    def __init__(self, file_path: Path | str, *, mode: str = "full"):
        if mode not in {"summary", "full"}:
            raise ValueError("MessageLogger mode must be 'summary' or 'full'")
        self.mode = mode
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(str(self._path), "a", encoding="utf-8")
        self._write_header()

    @property
    def path(self) -> Path:
        return self._path

    def _write_header(self) -> None:
        self._file.write(
            "cb-agent message log\n"
            f"mode: {self.mode}\n"
            f"started_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'=' * 70}\n"
        )
        self._file.flush()

    def log(
        self,
        messages: List[Dict[str, Any]],
        label: str = "",
    ) -> None:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        count = len(messages)
        total_chars = sum(len(_flat_content(m.get("content"))) for m in messages)
        full = self.mode == "full"

        lines: List[str] = [
            "",
            "-" * 70,
            f"[{now}] {label}  messages={count} chars={total_chars}",
            "-" * 70,
        ]

        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content")
            text = _flat_content(content)
            tag = f"[{i}] {str(role).upper()} chars={len(text)}"
            if role == "tool":
                tag += f" name={msg.get('name', '?')} call_id={msg.get('tool_call_id', '?')}"
            elif role == "assistant":
                tcs = msg.get("tool_calls") or []
                if tcs:
                    tc_names = [tc.get("function", {}).get("name", "?") for tc in tcs]
                    tag += f" tool_calls={tc_names}"
            lines.append("")
            lines.append(tag)

            if full:
                lines.append(text)
            else:
                lines.append(f"preview: {_clip(text)}")

            if role == "assistant":
                tcs = msg.get("tool_calls") or []
                if tcs:
                    lines.append(_tool_calls_text(tcs, full=full))

        self._file.write("\n".join(lines))
        self._file.flush()

    def close(self) -> None:
        try:
            self._file.write(f"\n{'=' * 70}\n")
            self._file.write(f"ended_at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            self._file.flush()
            self._file.close()
        except Exception:
            pass
