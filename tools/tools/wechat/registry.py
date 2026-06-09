"""WeChatTool 子功能注册表。

微信 OC 暴露的主动操作比 NapCat 少很多，首版只放模型真正需要的低频能力：
主动发文本、发图片/文件、发送输入状态、查看状态。openclaw-weixin 的 bot 存在于
当前微信账号私聊里，不是独立机器人账号，所以这里不再区分 root/普通用户。最终回答
和思考/工具事件仍然由事件渲染器自动发送，不需要模型每轮自己调用 send_text。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set


@dataclass(frozen=True)
class WeChatFunctionSpec:
    """一个 wechattool 子功能的元数据。"""

    funname: str
    action: str
    description: str
    required: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    root_only: bool = False
    current_conversation_only: bool = False
    file_param: str = ""
    result_limit: int = 20
    tags: Set[str] = field(default_factory=set)


def _spec(
    funname: str,
    action: str,
    description: str,
    *,
    required: Iterable[str] = (),
    aliases: Iterable[str] = (),
    root_only: bool = False,
    current_conversation_only: bool = False,
    file_param: str = "",
    result_limit: int = 20,
    tags: Iterable[str] = (),
) -> WeChatFunctionSpec:
    return WeChatFunctionSpec(
        funname=funname,
        action=action,
        description=description,
        required=tuple(required),
        aliases=tuple(aliases),
        root_only=root_only,
        current_conversation_only=current_conversation_only,
        file_param=file_param,
        result_limit=result_limit,
        tags=set(tags),
    )


WECHAT_FUNCTION_SPECS: Dict[str, WeChatFunctionSpec] = {
    "send_text": _spec(
        "send_text",
        "__cbagent_wechat_send_text__",
        "向当前微信会话发送一条文本消息",
        required=(),
        aliases=("send_message",),
        current_conversation_only=True,
        tags=("message",),
    ),
    "send_image": _spec(
        "send_image",
        "__cbagent_wechat_send_media__",
        "向当前微信会话发送图片或表情包",
        required=("path",),
        aliases=("send_sticker",),
        current_conversation_only=True,
        file_param="path",
        tags=("media", "file"),
    ),
    "send_file": _spec(
        "send_file",
        "__cbagent_wechat_send_media__",
        "向当前微信会话发送普通文件",
        required=("path",),
        current_conversation_only=True,
        file_param="path",
        tags=("media", "file"),
    ),
    "send_typing": _spec(
        "send_typing",
        "__cbagent_wechat_send_typing__",
        "向当前微信会话发送或取消输入状态",
        current_conversation_only=True,
        tags=("status",),
    ),
    "get_status": _spec(
        "get_status",
        "__cbagent_wechat_get_status__",
        "查看微信 transport 运行状态",
        tags=("status", "query"),
    ),
    "get_login_info": _spec(
        "get_login_info",
        "__cbagent_wechat_get_login_info__",
        "查看微信登录账号信息",
        tags=("account", "query"),
    ),
}


_ALIASES: Dict[str, str] = {}
for _name, _item in WECHAT_FUNCTION_SPECS.items():
    _ALIASES[_name.lower()] = _name
    for _alias in _item.aliases:
        _ALIASES[_alias.lower()] = _name


def get_wechat_function_spec(funname: str) -> WeChatFunctionSpec | None:
    canonical = _ALIASES.get(str(funname or "").strip().lower())
    if not canonical:
        return None
    return WECHAT_FUNCTION_SPECS.get(canonical)


def list_wechat_function_specs() -> List[WeChatFunctionSpec]:
    return list(WECHAT_FUNCTION_SPECS.values())


__all__ = ["WECHAT_FUNCTION_SPECS", "WeChatFunctionSpec", "get_wechat_function_spec", "list_wechat_function_specs"]
