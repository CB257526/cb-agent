"""Agent 事件类型集合

cb-agent 的事件流系统：所有从 AgentSession / LLMStream / ToolExecutor 流出的
"消息"统一表达为 dataclass 事件，再经 EventBus 派发给订阅者（CLI / TUI / Web）。

设计原则：
- 事件是**只读快照**，订阅者不要回写
- 字段全部用基础类型 + dataclass，便于后续 JSON 序列化（Web/SSE 推送）
- 事件按"时间发生序"被 emit，但跨类型不保证强顺序
- LLM 层关心的事件 vs 工具层关心的事件 vs Agent 层关心的事件，全部在这一个文件里集中定义
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


def _now() -> float:
    return time.time()


# ========== LLM 流式事件 ==========


@dataclass
class TextDelta:
    """模型流式 content 增量（assistant 正文）。"""
    delta: str
    accumulated: str = ""        # 当前轮累积全文（订阅者要 markdown 重渲染时用）
    round_idx: int = 0           # 工具循环的第几轮（1-based）
    timestamp: float = field(default_factory=_now)
    type: str = field(default="text_delta", init=False)


@dataclass
class ReasoningDelta:
    """模型流式 reasoning_content 增量（DeepSeek thinking 等）。"""
    delta: str
    accumulated: str = ""
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="reasoning_delta", init=False)


@dataclass
class TokenUsage:
    """流式响应结束时的 token 用量（最后一个非空 chunk.usage）。"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="token_usage", init=False)


# ========== 工具事件 ==========


@dataclass
class ToolCallPlanned:
    """LLM 流式累积出一个完整的 tool_call（finish_reason=tool_calls 时）。

    注意：这只是模型**计划**调用，工具还没真正执行。真正执行由 ToolExecutor 发 ToolStart。
    """
    call_id: str
    name: str
    arguments_json: str
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="tool_call_planned", init=False)


@dataclass
class ToolStart:
    """ToolExecutor 实际开始执行某个工具。"""
    call_id: str
    name: str
    arguments: Dict[str, Any]
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="tool_start", init=False)


@dataclass
class ToolComplete:
    """工具执行结束（成功或失败）。"""
    call_id: str
    name: str
    result: str                  # JSON 字符串（工具的 run() 返回值）
    duration_seconds: float
    is_error: bool = False
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="tool_complete", init=False)


# ========== 轮次事件 ==========


@dataclass
class RoundStart:
    """工具循环新一轮开始。"""
    round_idx: int
    max_rounds: int
    timestamp: float = field(default_factory=_now)
    type: str = field(default="round_start", init=False)


@dataclass
class RoundEnd:
    """一轮结束（要么模型给最终答案，要么进入下一轮）。"""
    round_idx: int
    has_tool_calls: bool         # 本轮是否有工具调用
    final: bool = False          # 是否本轮就是最终回答
    timestamp: float = field(default_factory=_now)
    type: str = field(default="round_end", init=False)


@dataclass
class Done:
    """整个 chat 调用结束（最终回答已就绪）。"""
    final_answer: str
    rounds_used: int
    timestamp: float = field(default_factory=_now)
    type: str = field(default="done", init=False)


# ========== 错误与中断 ==========


@dataclass
class Error:
    """非致命错误（LLM 调用失败、工具执行异常等）。"""
    where: str                   # 'llm' / 'tool' / 'session'
    message: str
    exception_type: Optional[str] = None
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="error", init=False)


@dataclass
class Cancelled:
    """用户中断（CancelToken 触发）。"""
    where: str                   # 'llm_stream' / 'tool' / 'between_rounds'
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="cancelled", init=False)


# ========== 后台任务通知 ==========


@dataclass
class BackgroundNotification:
    """drain 出来的后台任务完成通知，每轮 think 前注入。"""
    task_id: str
    status: str                  # 'done' / 'failed' / 'killed'
    exit_code: Optional[int]
    output_path: str
    timestamp: float = field(default_factory=_now)
    type: str = field(default="background_notification", init=False)


# ========== Union 类型（订阅者用 isinstance 区分）==========


Event = Union[
    TextDelta,
    ReasoningDelta,
    TokenUsage,
    ToolCallPlanned,
    ToolStart,
    ToolComplete,
    RoundStart,
    RoundEnd,
    Done,
    Error,
    Cancelled,
    BackgroundNotification,
]


__all__ = [
    "Event",
    "TextDelta",
    "ReasoningDelta",
    "TokenUsage",
    "ToolCallPlanned",
    "ToolStart",
    "ToolComplete",
    "RoundStart",
    "RoundEnd",
    "Done",
    "Error",
    "Cancelled",
    "BackgroundNotification",
]
