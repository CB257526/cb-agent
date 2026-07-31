"""工具执行协议、终态和分层超时策略。"""

from __future__ import annotations

import os
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from agent.cancel import CancellationContext
from core.media import ImageRef


class ToolCancellationMode(str, Enum):
    RUNTIME = "runtime"
    COOPERATIVE = "cooperative"
    BLOCKING = "blocking"


class ToolTerminalStatus(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED_BEFORE_START = "cancelled_before_start"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


class ToolEffectState(str, Enum):
    NONE = "none"
    COMPLETED = "completed"
    MAY_HAVE_OCCURRED = "may_have_occurred"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolModelResult:
    """工具同时返回文本终态和仅供模型使用的结构化内容。"""

    text: str
    content: tuple[Dict[str, Any], ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        """当前只允许可持久化 ImageRef，禁止工具绕过媒体安全边界。"""

        normalized: list[Dict[str, Any]] = []
        for part in self.content:
            if not isinstance(part, dict) or part.get("type") != "image_ref":
                raise ValueError("ToolModelResult 当前只支持 image_ref 内容块")
            ref = ImageRef.from_dict(part.get("image_ref") or {})
            # 重新生成规范字段，避免工具夹带未知键改变后续序列化结果。
            normalized.append({"type": "image_ref", "image_ref": ref.to_dict()})
        object.__setattr__(self, "text", str(self.text))
        object.__setattr__(self, "content", tuple(normalized))


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具调用的稳定身份与取消边界。"""

    turn_id: str
    round_idx: int
    call_id: str
    tool_name: str
    cancellation: CancellationContext
    deadline: Optional[float] = None

    def remaining_seconds(self) -> Optional[float]:
        return self.cancellation.remaining_seconds()

    def throw_if_cancelled(self) -> None:
        self.cancellation.throw_if_cancelled()


class ToolTimeoutPolicy:
    """解析单次调用、工具实例和全局默认值之间的超时优先级。"""

    def __init__(self, default_seconds: Optional[float] = None) -> None:
        if default_seconds is None:
            raw = os.environ.get("CB_AGENT_TOOL_TIMEOUT_SEC", "120")
            default_seconds = self._parse(raw, field="CB_AGENT_TOOL_TIMEOUT_SEC")
        self.default_seconds = default_seconds

    @staticmethod
    def _parse(value: Any, *, field: str) -> Optional[float]:
        if value is None:
            return None
        if isinstance(value, bool):
            raise ValueError(f"{field} 不能是布尔值")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} 必须是数字") from exc
        if not math.isfinite(number) or number < 0:
            raise ValueError(f"{field} 必须是大于等于 0 的有限数字")
        return None if number == 0 else number

    def resolve(
        self,
        *,
        tool_name: str,
        arguments: Dict[str, Any],
        tool_default_seconds: Any = ...,
    ) -> Optional[float]:
        # Bash 对外参数沿用毫秒；其他工具若未来需要单次覆盖，应在工具层转换。
        if tool_name == "bash" and "timeout" in arguments:
            value = self._parse(arguments.get("timeout"), field="bash.timeout")
            return None if value is None else value / 1000.0
        if tool_default_seconds is not ...:
            return self._parse(
                tool_default_seconds, field=f"{tool_name}.default_timeout_seconds"
            )
        return self.default_seconds

    @staticmethod
    def deadline_after(seconds: Optional[float]) -> Optional[float]:
        return None if seconds is None else time.monotonic() + seconds


__all__ = [
    "ToolCancellationMode",
    "ToolEffectState",
    "ToolExecutionContext",
    "ToolModelResult",
    "ToolTerminalStatus",
    "ToolTimeoutPolicy",
]
