"""QQ / NapCat OneBot V11 适配器包。

``QQNapCatAdapter`` 会间接加载 AgentSession / context / token 统计等重依赖。很多
单测或工具模块只需要 ``QQConfig``、OneBot 解析器、action bridge；因此这里用
``__getattr__`` 懒加载 adapter，避免普通导入也触发完整 agent 依赖。
"""

from .config import QQConfig
from .onebot import parse_onebot_event, parse_onebot_message_event


def __getattr__(name: str):
    if name == "QQNapCatAdapter":
        from .adapter import QQNapCatAdapter

        return QQNapCatAdapter
    raise AttributeError(name)


__all__ = ["QQConfig", "QQNapCatAdapter", "parse_onebot_event", "parse_onebot_message_event"]
