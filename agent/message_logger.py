"""完整对话消息日志；图片正文始终在写盘前脱敏。"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from agent.multimodal_input import sanitize_multimodal_payload


def _json_default(value: Any) -> str:
    return str(value)


class MessageLogger:
    def __init__(self, file_path: Path | str, *, mode: str = "full"):
        if mode != "full":
            raise ValueError("MessageLogger only supports full mode")
        self.mode = mode
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(str(self._path), "a", encoding="utf-8")
        self._write_record("log_start", {
            "mode": self.mode,
            "started_at": datetime.now().isoformat(timespec="milliseconds"),
        })

    @property
    def path(self) -> Path:
        return self._path

    def _write_record(self, event: str, payload: Dict[str, Any]) -> None:
        # 脱敏必须由日志器自身强制执行。调用方即使误传原始 provider payload，
        # data URI 也不能进入持久化日志；普通长文本仍保持完整，便于检查组装。
        record = sanitize_multimodal_payload({"event": event, **payload})
        self._file.write(json.dumps(record, ensure_ascii=False, default=_json_default))
        self._file.write("\n")
        self._file.flush()

    def log(
        self,
        messages: List[Dict[str, Any]],
        label: str = "",
        *,
        tools: List[Dict[str, Any]] | None = None,
        response: Any = None,
    ) -> None:
        self._write_record("messages", {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "label": label,
            "message_count": len(messages),
            "messages": messages,
            "tools": tools,
            "response": response,
        })

    def close(self) -> None:
        try:
            self._write_record("log_end", {
                "ended_at": datetime.now().isoformat(timespec="milliseconds"),
            })
            self._file.close()
        except Exception:
            pass
