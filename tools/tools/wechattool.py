"""微信 OC 平台专用聚合工具。"""

from __future__ import annotations

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
                "通过 funname 指定要执行的微信操作，通过 args 传入参数。"
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
                    "子功能参数对象。当前私聊会话内可省略 user_id；不要传 group_id。"
                    "send_text 传 text，send_image/send_file 传 path，send_typing 可传 cancel=true。"
                ),
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not str((parameters or {}).get("funname") or "").strip():
            return False
        return isinstance((parameters or {}).get("args"), dict)

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return dumps_result({
                "ok": False,
                "funname": str((parameters or {}).get("funname") or ""),
                "action": "",
                "error": "参数无效：必须提供 funname 和对象形式的 args",
                "duration_ms": 0,
            })
        return dumps_result(run_wechat_function(
            str(parameters.get("funname") or ""),
            dict(parameters.get("args") or {}),
        ))


__all__ = ["WeChatTool"]
