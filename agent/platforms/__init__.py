"""通讯平台抽象层。

这一层不关心 QQ、微信、Telegram 的具体 API，只定义 cb-agent 内部统一使用的
消息结构和事件渲染规则。具体平台只需要把这些结构翻译成自己的收发接口。
"""

from .messages import (
    ConversationKey,
    InboundAttachment,
    InboundMessage,
    OutboundMessage,
    OutboundSegment,
)
from .renderer import PlatformEventRenderer

__all__ = [
    "ConversationKey",
    "InboundAttachment",
    "InboundMessage",
    "OutboundMessage",
    "OutboundSegment",
    "PlatformEventRenderer",
]
