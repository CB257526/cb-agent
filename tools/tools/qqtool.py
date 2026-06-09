"""QQ / NapCat 平台专用聚合工具。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from tools.tool import Tool
from tools.toolParameter import ToolParameter
from tools.tools.qq.functions import QQ_FUNCTIONS, dumps_result, run_qq_function


class QQTool(Tool):
    """通过 funname 分发 NapCat/OneBot QQ 操作。"""

    def __init__(self) -> None:
        names = ", ".join(item.funname for item in QQ_FUNCTIONS)
        super().__init__(
            name="qqtool",
            description=(
                "QQ/NapCat 平台专用工具，只在 QQ transport 中可用。"
                "必须通过 funname 指定要执行的 QQ 操作，通过 args 传入参数对象。"
                "调用格式必须是 {\"funname\":\"...\",\"args\":{...}}，args 必须是 object，不要把 args 写成 JSON 字符串。"
                "示例：发送群文本 {\"funname\":\"send_group_msg\",\"args\":{\"group_id\":123,\"message\":\"hello\"}}；"
                "发送群图片 {\"funname\":\"send_group_msg\",\"args\":{\"group_id\":123,\"message\":[{\"type\":\"image\",\"data\":{\"file\":\"/tmp/cb-agent-outputs/a.png\"}}]}}；"
                "上传群文件 {\"funname\":\"upload_group_file\",\"args\":{\"group_id\":123,\"file\":\"/tmp/cb-agent-outputs/report.pdf\",\"name\":\"report.pdf\"}}；"
                "戳一戳 {\"funname\":\"send_poke\",\"args\":{\"group_id\":123,\"user_id\":456}}。"
                "当前 QQ 群聊或私聊内，如果省略当前会话自身的 group_id/user_id，系统会尽量从 ConversationKey 安全补齐。"
                "发送图片或文件时直接传本地临时产物路径，不需要手动调用 __cbagent_prepare_resource_reference__；"
                "如果参数已经是 QQ_FILE_NAPCAT_PREFIX 下的容器映射路径，也会直接透传。"
                "模型的最终回答、思考内容、工具过程提示、ask_user_question 仍由系统事件自动发送；"
                "只有需要主动执行 QQ 操作时才调用本工具。"
                "普通用户只能操作当前会话和临时产物，root 用户才可跨会话、读历史、改资料或 raw_action。"
                "支持的 funname 包括：" + names
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="funname",
                type="string",
                required=True,
                description=(
                    "QQ 子功能名称，例如 send_poke、send_group_msg、upload_group_file、"
                    "get_group_member_list、get_login_info、raw_action。"
                ),
            ),
            ToolParameter(
                name="args",
                type="object",
                required=True,
                description=(
                    "子功能参数对象，必须直接传 object，不能传字符串。"
                    "正确：{\"funname\":\"send_group_msg\",\"args\":{\"group_id\":123,\"message\":\"hello\"}}。"
                    "错误：{\"funname\":\"send_group_msg\",\"args\":\"{\\\"group_id\\\":123}\"}。"
                    "消息类通常传 user_id/group_id/message；图片消息段用 message=[{\"type\":\"image\",\"data\":{\"file\":\"/path/a.png\"}}]；"
                    "也兼容 [CQ:image,file=/path/a.png] 这类 CQ 字符串。"
                    "文件类传 file/name；raw_action 传 action 和 params。"
                ),
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not str((parameters or {}).get("funname") or "").strip():
            return False
        return isinstance(_coerce_args((parameters or {}).get("args")), dict)

    def run(self, parameters: Dict[str, Any]) -> str:
        clean_args = _coerce_args((parameters or {}).get("args"))
        if not str((parameters or {}).get("funname") or "").strip() or not isinstance(clean_args, dict):
            return dumps_result({
                "ok": False,
                "funname": str((parameters or {}).get("funname") or ""),
                "action": "",
                "error": (
                    "参数无效：必须提供 funname 和 object 形式的 args。"
                    "正确格式示例：{\"funname\":\"send_group_msg\",\"args\":{\"group_id\":123,\"message\":\"hello\"}}。"
                    "不要把 args 写成字符串，例如 args=\"{...}\"。"
                ),
                "duration_ms": 0,
            })
        payload = run_qq_function(
            str(parameters.get("funname") or ""),
            dict(clean_args),
        )
        if isinstance((parameters or {}).get("args"), str):
            payload.setdefault("metadata", {})
            if isinstance(payload["metadata"], dict):
                payload["metadata"]["args_auto_parsed"] = True
        return dumps_result(payload)


def _coerce_args(raw: Any) -> Any:
    """兼容模型把 args 对象误写成 JSON 字符串的情况。

    schema 里已经声明 ``args`` 是 object，但部分模型仍会把对象二次 JSON 编码成字符串。
    与其让模型在工具循环里反复试错，不如在入口做一次安全解析；解析失败仍返回明确
    错误，提醒下一轮使用 object。
    """

    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return raw
        return parsed
    return raw


__all__ = ["QQTool"]
