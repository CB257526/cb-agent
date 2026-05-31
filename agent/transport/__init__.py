"""cb-agent transport 模块

把 agent 的事件流通过 stdio JSON-RPC 暴露出去，让任何外部 UI 进程都能订阅。
设计参考 Hermes 的 tui_gateway，但裁掉了多 session、长 RPC 线程池、TeeTransport
等 cb-agent 用不到的复杂度。

模块划分：
- jsonrpc.py: 协议层（NDJSON 分帧 + JSON-RPC 2.0 envelope）
- gateway.py: 业务层（订阅 EventBus + 处理 RPC + 调度 chat）
"""

from agent.transport.gateway import Gateway
from agent.transport.jsonrpc import StdioTransport, make_event_message, make_response

__all__ = ["Gateway", "StdioTransport", "make_event_message", "make_response"]
