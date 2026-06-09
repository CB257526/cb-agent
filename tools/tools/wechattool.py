"""微信 OC 平台专用聚合工具。"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from tools.tool import Tool
from tools.toolParameter import ToolParameter
from tools.tools.wechat.functions import WECHAT_FUNCTIONS, dumps_result, run_wechat_function


class WeChatTool(Tool):
    """通过 funname 分发微信 OC 主动操作。"""

    def __init__(self) -> None:
        names = ", ".join(item.funname for item in WECHAT_FUNCTIONS)
        super().__init__(
            name="wechattool",
            description=(
                "微信 OC 平台专用工具，只在 wechat transport 中可用。"
                "必须通过 funname 指定要执行的微信操作，通过 args 传入参数对象。"
                "调用格式必须是 {\"funname\":\"...\",\"args\":{...}}，args 必须是 object，不要把 args 写成 JSON 字符串。"
                "示例：发送文本 {\"funname\":\"send_text\",\"args\":{\"text\":\"hello\"}}；"
                "发送图片 {\"funname\":\"send_image\",\"args\":{\"path\":\"/tmp/cb-agent-outputs/a.png\"}}。"
                "最终回答、思考内容、工具过程提示、ask_user_question 编号问题仍由系统事件自动发送；"
                "只有需要主动发送额外文本、图片、文件或输入状态时才调用本工具。"
                "微信 OC 是当前账号里的私聊 bot，不区分 root/普通用户，也不支持微信群聊操作。"
                "支持的 funname 包括：" + names
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="funname",
                type="string",
                required=True,
                description="微信子功能名称，例如 send_text、send_image、send_file、send_typing、get_status。",
            ),
            ToolParameter(
                name="args",
                type="object",
                required=True,
                description=(
                    "子功能参数对象，必须直接传 object，不能传字符串。当前私聊会话内可省略 user_id；不要传 group_id。"
                    "send_text 传 text，send_image/send_file 传 path，send_typing 可传 cancel=true。"
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
                    "正确格式示例：{\"funname\":\"send_text\",\"args\":{\"text\":\"hello\"}}。"
                    "不要把 args 写成字符串，例如 args=\"{...}\"。"
                ),
                "duration_ms": 0,
            })
        payload = run_wechat_function(
            str(parameters.get("funname") or ""),
            dict(clean_args),
        )
        if isinstance((parameters or {}).get("args"), str):
            payload.setdefault("metadata", {})
            if isinstance(payload["metadata"], dict):
                payload["metadata"]["args_auto_parsed"] = True
        return dumps_result(payload)


def _coerce_args(raw: Any) -> Any:
    """兼容模型把 args 对象误写成 JSON 字符串的情况。"""

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


__all__ = ["WeChatTool"]
