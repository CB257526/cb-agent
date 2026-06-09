"""QQ / NapCat 平台专用聚合工具。"""

from __future__ import annotations

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
                "通过 funname 指定要执行的 QQ 操作，通过 args 传入参数。"
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
                    "子功能参数对象。消息类通常传 user_id/group_id/message；文件类传 file/name；"
                    "raw_action 传 action 和 params。"
                ),
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not str((parameters or {}).get("funname") or "").strip():
            return False
        args = (parameters or {}).get("args")
        return isinstance(args, dict)

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return dumps_result({
                "ok": False,
                "funname": str((parameters or {}).get("funname") or ""),
                "action": "",
                "error": "参数无效：必须提供 funname 和对象形式的 args",
                "duration_ms": 0,
            })
        payload = run_qq_function(
            str(parameters.get("funname") or ""),
            dict(parameters.get("args") or {}),
        )
        return dumps_result(payload)


__all__ = ["QQTool"]
