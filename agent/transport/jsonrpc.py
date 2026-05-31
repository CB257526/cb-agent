"""JSON-RPC 2.0 over NDJSON 协议层。

帧格式（参考 Hermes）：
- 每条消息 = 一行 JSON object + '\n'
- 不用 LSP 的 Content-Length header（NDJSON 简单够用，UTF-8 也安全）

消息类型：
- 事件推送（gateway → UI）：method="event"，无 id（notification）
    {"jsonrpc": "2.0", "method": "event", "params": {"type": "...", "round_idx": 1, ...}}
- RPC 请求（UI → gateway）：带 id
    {"jsonrpc": "2.0", "id": "abc", "method": "prompt.submit", "params": {...}}
- RPC 响应（gateway → UI）：带相同 id
    {"jsonrpc": "2.0", "id": "abc", "result": {...}}      # 成功
    {"jsonrpc": "2.0", "id": "abc", "error": {"code": -32603, "message": "..."}}  # 失败

事件 type 复用 cb-agent 现有 events.py 的 type 字段（text_delta / tool_start /
tool_complete / done / error / cancelled / round_start / ...），不重新定义一套，
减少字典查找和心智负担。
"""

from __future__ import annotations

import dataclasses
import json
import sys
import threading
from typing import Any, Dict, Iterator, Optional, TextIO

from agent.events import Event


def _event_to_dict(event: Event) -> Dict[str, Any]:
    """dataclass 事件 → 可 JSON 序列化 dict。

    `dataclasses.asdict` 已经处理了嵌套，这里只做一件事：把 `arguments` 这种
    Dict[str, Any] 字段里可能出现的非 JSON 类型（实际上 cb-agent 工具入参全是
    str/int/float/bool/list/dict 没问题）兜底成 str。
    """
    d = dataclasses.asdict(event)
    return d


def make_event_message(event: Event) -> Dict[str, Any]:
    """事件 → JSON-RPC notification。"""
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": _event_to_dict(event),
    }


def make_response(
    rpc_id: Any,
    *,
    result: Any = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造 JSON-RPC 2.0 response。result / error 必须给且只给一个。"""
    msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result if result is not None else {}
    return msg


class StdioTransport:
    """读 stdin 写 stdout 的 NDJSON 传输层。

    线程安全：write 用锁保护（事件可能在工具线程 / chat 线程 / RPC 线程并发产生）。
    read_loop() 是阻塞迭代器，调用方自己决定跑在哪个线程。
    """

    def __init__(
        self,
        stdin: Optional[TextIO] = None,
        stdout: Optional[TextIO] = None,
    ) -> None:
        self._stdin = stdin if stdin is not None else sys.stdin
        self._stdout = stdout if stdout is not None else sys.stdout
        self._write_lock = threading.Lock()
        self._closed = False

    def write(self, msg: Dict[str, Any]) -> bool:
        """写一条消息。peer 关闭管道（EPIPE/BrokenPipeError）返回 False，调用方据此停。"""
        if self._closed:
            return False
        line = json.dumps(msg, ensure_ascii=False) + "\n"
        try:
            with self._write_lock:
                self._stdout.write(line)
                self._stdout.flush()
            return True
        except (BrokenPipeError, OSError):
            self._closed = True
            return False

    def read_loop(self) -> Iterator[Dict[str, Any]]:
        """阻塞迭代 stdin，每行 yield 一条 JSON 消息。

        - 空行跳过
        - 解析失败：写一条标准 JSON-RPC parse error 到 stdout，继续读
        - stdin EOF：迭代结束
        """
        for raw in self._stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                # 协议错：parse error (-32700)
                self.write(make_response(
                    None,
                    error={"code": -32700, "message": f"parse error: {e}"},
                ))
                continue
            if not isinstance(msg, dict):
                self.write(make_response(
                    None,
                    error={"code": -32600, "message": "invalid request: not an object"},
                ))
                continue
            yield msg

    def close(self) -> None:
        self._closed = True
