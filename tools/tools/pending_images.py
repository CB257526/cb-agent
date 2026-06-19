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

线程安全：工具在 ToolExecutor 的 worker 线程里执行，循环在主线程 drain，
所以读写都用 Lock 保护。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List

_lock = threading.Lock()
_pending: List[Dict[str, Any]] = []


def queue_image(*, call_id: str, image_part: Dict[str, Any], file_name: str) -> None:
    """工具侧：把一个 image_url 内容块排队，等工具循环注入成 user 消息。

    Args:
        call_id: 触发本次加载的 tool_call id，仅用于日志关联（可空）。
        image_part: OpenAI 内容块，形如
            {"type": "image_url", "image_url": {"url": "<data uri 或 http url>"}}。
        file_name: 图片文件名/URL 末段，用于注入消息里的文本说明。
    """
    with _lock:
        _pending.append({
            "call_id": call_id,
            "image_part": image_part,
            "file_name": file_name,
        })


def drain_images() -> List[Dict[str, Any]]:
    """循环侧：取出并清空当前所有排队图片。无图片时返回空列表。

    工具循环每执行完一批工具就调一次（在全部 role=tool 消息回灌之后），所以
    缓冲里的图片总在同一轮被取走注入，不会跨轮/跨会话残留——无需额外的清理钩子。
    """
    with _lock:
        items = _pending[:]
        _pending.clear()
    return items
