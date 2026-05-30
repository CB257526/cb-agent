"""文件读状态注册表 — file_read 和 file_write 共享。

用途：file_write 在覆盖已有文件前要确认两点：
1. 该路径在本进程内被 file_read 读过（避免模型盲写）
2. 自那次读取以来文件未被外部进程修改（避免覆盖 linter/用户的并发改动）

这是 Claude Code FileWriteTool 的 staleness check 的最小搬运。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Dict, Optional


class ReadStateRegistry:
    """进程级单例。线程安全，键为绝对规范化路径。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # path -> 当时记录的 mtime_ns（用 ns 精度，规避 fat32 1s 抖动）
        self._reads: Dict[str, int] = {}

    @staticmethod
    def _key(path: Path) -> str:
        return str(path.resolve()).lower() if os.name == "nt" else str(path.resolve())

    def mark_read(self, path: Path) -> None:
        """读取成功时调用。把路径的当前 mtime 记下来。"""
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            return  # 文件没了就别记，留给写入端 ENOENT 路径
        with self._lock:
            self._reads[self._key(path)] = mtime_ns

    def get_read_mtime(self, path: Path) -> Optional[int]:
        """返回上次记录的 mtime_ns，未记录返回 None。"""
        with self._lock:
            return self._reads.get(self._key(path))

    def clear(self) -> None:
        """测试用。"""
        with self._lock:
            self._reads.clear()


_instance: Optional[ReadStateRegistry] = None


def get_read_state_registry() -> ReadStateRegistry:
    global _instance
    if _instance is None:
        _instance = ReadStateRegistry()
    return _instance
