"""load_image 工具与 tool loop 之间的图片传递缓冲（进程内）。

为什么需要这个缓冲：
  工具的 run() 按 OpenAI 协议返回字符串，最终落进 role="tool" 消息。但多模态
  模型要"看到"图片，必须把 image_url 放进 role="user"/"system" 消息——大多数
  OpenAI 兼容中转站不接受 role="tool" 里塞 image_url。所以多模态分支不能靠
  返回值把图片带给模型。

  解决办法（对齐 codex 的 view_image 在 Chat Completions 协议下的等价做法）：
  load_image 把图片内容块塞进这个缓冲，工具返回值只放一句文本确认；
  AgentSession 的工具循环在执行完一批工具后 drain 缓冲，把图片作为一条
  role="user" 消息注入当轮 messages。base64 只存在于当轮请求，不进 history
  （与用户附件图片同一条安全边界）。

线程与会话隔离：每次 AgentSession.chat 绑定独立缓冲，ToolExecutor 通过
ContextVar 把同一缓冲传播到 worker；缓冲内部再用 Lock 保护并发读写。
"""

from __future__ import annotations

import threading
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class _PendingImageBuffer:
    """单次 AgentSession.chat 使用的线程安全图片缓冲。"""

    lock: threading.Lock = field(default_factory=threading.Lock)
    items: List[Dict[str, Any]] = field(default_factory=list)


_fallback_buffer = _PendingImageBuffer()
_current_buffer: ContextVar[_PendingImageBuffer | None] = ContextVar(
    "cbagent_pending_image_buffer",
    default=None,
)


def set_pending_image_buffer() -> Token[_PendingImageBuffer | None]:
    """为当前会话绑定新缓冲；ToolExecutor 会把同一对象传播到工具线程。"""

    return _current_buffer.set(_PendingImageBuffer())


def reset_pending_image_buffer(token: Token[_PendingImageBuffer | None]) -> None:
    """恢复进入当前会话前的图片缓冲上下文。"""

    _current_buffer.reset(token)


def _buffer() -> _PendingImageBuffer:
    """返回当前会话缓冲；直接调用工具时退回兼容用进程缓冲。"""

    return _current_buffer.get() or _fallback_buffer


def queue_image(*, call_id: str, image_part: Dict[str, Any], file_name: str) -> None:
    """工具侧：把一个 image_url 内容块排队，等工具循环注入成 user 消息。

    Args:
        call_id: 触发本次加载的 tool_call id，仅用于日志关联（可空）。
        image_part: OpenAI 内容块，形如
            {"type": "image_url", "image_url": {"url": "<data uri 或 http url>"}}。
        file_name: 图片文件名/URL 末段，用于注入消息里的文本说明。
    """
    buffer = _buffer()
    with buffer.lock:
        buffer.items.append({
            "call_id": call_id,
            "image_part": image_part,
            "file_name": file_name,
        })


def drain_images() -> List[Dict[str, Any]]:
    """循环侧：取出并清空当前所有排队图片。无图片时返回空列表。

    工具循环每执行完一批工具就调一次（在全部 role=tool 消息回灌之后），所以
    缓冲里的图片总在同一轮被取走注入；不同 chat 使用独立上下文，不会跨会话
    互相取走图片。直接调用工具时使用兼容缓冲，仍由调用方负责及时 drain。
    """
    buffer = _buffer()
    with buffer.lock:
        items = buffer.items[:]
        buffer.items.clear()
    return items
