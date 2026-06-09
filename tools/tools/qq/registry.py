"""QQTool 子功能注册表。

所有 NapCat action 映射集中放在这里，后续 Apifox 文档更新时只需要改一处。模型
看到的是稳定的 ``funname``，内部再映射到真实 OneBot/NapCat action。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Set


@dataclass(frozen=True)
class QQFunctionSpec:
    """一个 qqtool 子功能的元数据。"""

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
) -> QQFunctionSpec:
    return QQFunctionSpec(
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


QQ_FUNCTION_SPECS: Dict[str, QQFunctionSpec] = {
    # 消息/互动。普通用户只允许操作当前会话；跨会话由权限层判为敏感。
    "send_private_msg": _spec(
        "send_private_msg", "send_private_msg", "发送私聊消息",
        required=("user_id", "message"), current_conversation_only=True, tags=("message",),
    ),
    "send_group_msg": _spec(
        "send_group_msg", "send_group_msg", "发送群消息",
        required=("group_id", "message"), current_conversation_only=True, tags=("message",),
    ),
    "send_poke": _spec(
        "send_poke", "send_poke", "发送戳一戳动作",
        required=("user_id",), current_conversation_only=True, aliases=("group_poke", "friend_poke"), tags=("message",),
    ),

    # 文件/媒体。临时产物和 sticker 可给普通用户用，任意路径由权限层拦截。
    "upload_private_file": _spec(
        "upload_private_file", "upload_private_file", "上传私聊文件",
        required=("user_id", "file"), current_conversation_only=True, file_param="file", tags=("file",),
    ),
    "upload_group_file": _spec(
        "upload_group_file", "upload_group_file", "上传群文件",
        required=("group_id", "file"), current_conversation_only=True, file_param="file", tags=("file",),
    ),
    "upload_image_to_qun_album": _spec(
        "upload_image_to_qun_album", "upload_image_to_qun_album", "上传图片到群相册",
        required=("group_id", "file"), current_conversation_only=True, file_param="file", tags=("file", "album"),
    ),

    # 群信息与群扩展。
    "get_group_list": _spec("get_group_list", "get_group_list", "获取群列表", root_only=True, tags=("group", "query")),
    "get_group_info": _spec(
        "get_group_info", "get_group_info", "获取群信息",
        required=("group_id",), current_conversation_only=True, tags=("group", "query"),
    ),
    "get_group_info_ex": _spec(
        "get_group_info_ex", "get_group_info_ex", "获取群详细信息",
        required=("group_id",), current_conversation_only=True, aliases=("get_group_detail_info",), tags=("group", "query"),
    ),
    "get_group_member_list": _spec(
        "get_group_member_list", "get_group_member_list", "获取群成员列表",
        required=("group_id",), current_conversation_only=True, tags=("group", "query"),
    ),
    "get_group_member_info": _spec(
        "get_group_member_info", "get_group_member_info", "获取群成员信息",
        required=("group_id", "user_id"), current_conversation_only=True, tags=("group", "query"),
    ),
    "send_group_sign": _spec(
        "send_group_sign", "send_group_sign", "群打卡",
        required=("group_id",), current_conversation_only=True, tags=("group",),
    ),
    "get_group_signed_list": _spec(
        "get_group_signed_list", "get_group_signed_list", "获取群组今日打卡列表",
        required=("group_id",), current_conversation_only=True, tags=("group", "query"),
    ),
    "get_qun_album_list": _spec(
        "get_qun_album_list", "get_qun_album_list", "获取群相册列表",
        required=("group_id",), current_conversation_only=True, tags=("group", "album", "query"),
    ),
    "get_group_album_media_list": _spec(
        "get_group_album_media_list", "get_group_album_media_list", "获取群相册媒体列表",
        required=("group_id",), current_conversation_only=True, tags=("group", "album", "query"),
    ),

    # 好友/账号查询。好友列表和最近会话会泄露账号社交图谱，默认 root-only。
    "get_login_info": _spec("get_login_info", "get_login_info", "获取登录号信息", tags=("account", "query")),
    "send_like": _spec(
        "send_like", "send_like", "给指定用户点赞",
        required=("user_id",), current_conversation_only=True, tags=("friend",),
    ),
    "get_friend_list": _spec("get_friend_list", "get_friend_list", "获取当前帐号好友列表", root_only=True, tags=("friend", "query")),
    "get_friends_with_category": _spec("get_friends_with_category", "get_friends_with_category", "获取带分组的好友列表", root_only=True, tags=("friend", "query")),
    "get_recent_contact": _spec("get_recent_contact", "get_recent_contact", "获取最近会话", root_only=True, tags=("friend", "query")),

    # 历史消息默认敏感。
    "get_group_msg_history": _spec(
        "get_group_msg_history", "get_group_msg_history", "获取群历史消息",
        required=("group_id",), root_only=True, tags=("history", "query"),
    ),
    "get_friend_msg_history": _spec(
        "get_friend_msg_history", "get_friend_msg_history", "获取好友历史消息",
        required=("user_id",), root_only=True, tags=("history", "query"),
    ),
    "get_forward_msg": _spec(
        "get_forward_msg", "get_forward_msg", "获取合并转发消息",
        required=("message_id",), root_only=True, tags=("history", "query"),
    ),

    # 账号资料/Ark 属于账号状态修改或跨会话分享，默认 root-only。
    "set_self_longnick": _spec("set_self_longnick", "set_self_longnick", "设置个性签名", required=("longNick",), root_only=True, tags=("account",)),
    "set_qq_profile": _spec("set_qq_profile", "set_qq_profile", "设置 QQ 资料", root_only=True, tags=("account",)),
    "_set_model_show": _spec("_set_model_show", "_set_model_show", "设置机型", root_only=True, aliases=("set_model_show",), tags=("account",)),
    "ArkShareGroup": _spec("ArkShareGroup", "ArkShareGroup", "分享群 Ark", required=("group_id",), root_only=True, tags=("ark",)),
    "ArkSharePeer": _spec("ArkSharePeer", "ArkSharePeer", "分享用户 Ark", required=("user_id",), root_only=True, tags=("ark",)),

    # P1 常用只读/低风险接口。
    "can_send_image": _spec("can_send_image", "can_send_image", "是否可以发送图片", tags=("status", "query")),
    "can_send_record": _spec("can_send_record", "can_send_record", "是否可以发送语音", tags=("status", "query")),
    "get_version_info": _spec("get_version_info", "get_version_info", "获取版本信息", tags=("status", "query")),
    "get_status": _spec("get_status", "get_status", "获取运行状态", tags=("status", "query")),
    "get_group_at_all_remain": _spec(
        "get_group_at_all_remain", "get_group_at_all_remain", "获取群 @全体 剩余次数",
        required=("group_id",), current_conversation_only=True, tags=("group", "query"),
    ),
    "set_group_todo": _spec(
        "set_group_todo", "set_group_todo", "设置群待办",
        required=("group_id", "message_id"), current_conversation_only=True, tags=("group", "todo"),
    ),
    "complete_group_todo": _spec(
        "complete_group_todo", "complete_group_todo", "完成群待办",
        required=("group_id", "message_id"), current_conversation_only=True, tags=("group", "todo"),
    ),
    "cancel_group_todo": _spec(
        "cancel_group_todo", "cancel_group_todo", "取消群待办",
        required=("group_id", "message_id"), current_conversation_only=True, tags=("group", "todo"),
    ),
    "set_msg_emoji_like": _spec("set_msg_emoji_like", "set_msg_emoji_like", "设置消息表情点赞", required=("message_id",), tags=("message",)),
    "fetch_emoji_like": _spec("fetch_emoji_like", "fetch_emoji_like", "获取消息表情点赞列表", required=("message_id",), tags=("message", "query")),
    "get_mini_app_ark": _spec("get_mini_app_ark", "get_mini_app_ark", "获取小程序 Ark", root_only=True, tags=("ark", "query")),
    "get_online_clients": _spec("get_online_clients", "get_online_clients", "获取用户在线状态", root_only=True, tags=("account", "query")),

    # 兜底调试入口。它可以调用任意 NapCat action，必须 root-only。
    "raw_action": _spec("raw_action", "", "调用任意 NapCat action，调试兜底入口", required=("action",), root_only=True, tags=("raw",)),
}


_ALIASES: Dict[str, str] = {}
for _name, _item in QQ_FUNCTION_SPECS.items():
    _ALIASES[_name.lower()] = _name
    for _alias in _item.aliases:
        _ALIASES[_alias.lower()] = _name


def get_qq_function_spec(funname: str) -> QQFunctionSpec | None:
    """按名称或别名查找子功能定义。"""

    canonical = _ALIASES.get(str(funname or "").strip().lower())
    if not canonical:
        return None
    return QQ_FUNCTION_SPECS.get(canonical)


def list_qq_function_specs() -> List[QQFunctionSpec]:
    """返回全部子功能定义。"""

    return list(QQ_FUNCTION_SPECS.values())


__all__ = ["QQFunctionSpec", "QQ_FUNCTION_SPECS", "get_qq_function_spec", "list_qq_function_specs"]
