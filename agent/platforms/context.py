"""通讯平台运行上下文。

同一个 EventBus 会被 OTUI/QQ/微信共用。QQ 模式如果允许多个群聊或私聊并发运行，
事件本身又没有携带会话 ID，就必须借助 ContextVar 记录“当前这次 chat 属于哪个
通讯会话”。ToolExecutor 会复制 contextvars 到工具线程，因此工具事件也能沿用同
一个 ConversationKey。
"""

from __future__ import annotations

import contextvars
from typing import Optional

from agent.platforms.messages import ConversationKey


_current_conversation: contextvars.ContextVar[Optional[ConversationKey]] = contextvars.ContextVar(
    "cb_agent_platform_conversation",
    default=None,
)
_current_sender_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "cb_agent_platform_sender_id",
    default=None,
)


def set_current_platform_conversation(conversation: Optional[ConversationKey]) -> contextvars.Token:
    """把当前执行流绑定到一个通讯软件会话。"""

    return _current_conversation.set(conversation)


def get_current_platform_conversation() -> Optional[ConversationKey]:
    """读取当前执行流所属的通讯软件会话；本地 OTUI 模式返回 None。"""

    return _current_conversation.get()


def set_current_platform_sender(sender_id: Optional[str]) -> contextvars.Token:
    """把当前执行流绑定到通讯软件消息的发送者。"""

    return _current_sender_id.set(str(sender_id) if sender_id is not None else None)


def get_current_platform_sender() -> Optional[str]:
    """读取当前触发 agent 的平台用户 ID。"""

    return _current_sender_id.get()


def reset_current_platform_conversation(token: contextvars.Token) -> None:
    """恢复 set_current_platform_conversation 之前的上下文值。"""

    _current_conversation.reset(token)


def reset_current_platform_sender(token: contextvars.Token) -> None:
    """恢复 set_current_platform_sender 之前的上下文值。"""

    _current_sender_id.reset(token)


__all__ = [
    "get_current_platform_conversation",
    "get_current_platform_sender",
    "reset_current_platform_conversation",
    "reset_current_platform_sender",
    "set_current_platform_conversation",
    "set_current_platform_sender",
]
