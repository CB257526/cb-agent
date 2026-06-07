"""QQ / NapCat OneBot V11 适配器。"""

from .adapter import QQNapCatAdapter
from .config import QQConfig
from .onebot import parse_onebot_event, parse_onebot_message_event

__all__ = ["QQConfig", "QQNapCatAdapter", "parse_onebot_event", "parse_onebot_message_event"]
