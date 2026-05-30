"""bash_permission 工具：直接管理 bash 命令的 allowlist

让模型可以把用户自然语言授权（"以后 X 命令不要再问我"）转化成具体的
allowlist 写入，不需要等弹窗 + 用户按按钮。

四个 action：
- list：列当前所有规则（cwd / global / session 三档）
- grant：加规则
- revoke：删规则
- check：查某个 prefix 在某 cwd 下是否已被允许（模型自检用）

设计取舍：
- 不允许 grant fatal 命令（rm -rf /、Invoke-Expression 等）
- 不允许 grant 空 prefix
- grant 同样规则不报错，幂等（add_rule 接受重复）
- scope 默认 "cwd"，匹配用户最常说的"在这个项目里"
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from tools.tools.bash_permission import (
    PermissionGate, get_permission_gate,
)
from tools.tools.bash_session import get_session


# 黑名单：这些前缀禁止写入 allowlist。即使用户说"以后 rm 不要问我"，
# 也走弹窗确认，防止模型被诱导给自己开后门。
DENY_PREFIXES: set = {
    "rm", "del", "erase", "rd", "rmdir",
    "remove-item", "ri",
    "format-volume", "clear-disk",
    "mkfs", "dd",
    "iex", "invoke-expression",
    "curl", "wget",  # 远程脚本入口
    "sudo", "su",
}


class BashPermissionTool(Tool):
    def __init__(self, gate: Optional[PermissionGate] = None):
        super().__init__(
            name="bash_permission",
            description=(
                "管理 bash 命令的 allowlist（用户授权列表）。"
                "当用户说'以后 X 命令不要再问我'/'授权 X'/'撤销 X 授权'时调用。"
                "支持 list / grant / revoke / check 四个 action。"
                "高危命令（rm / Remove-Item / curl / sudo 等）禁止写入 allowlist，"
                "仍走弹窗。"
            ),
        )
        self._gate = gate or get_permission_gate()

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="list / grant / revoke / check 之一。",
                required=True,
            ),
            ToolParameter(
                name="prefix",
                type="string",
                description=(
                    "命令前缀。单 token 命令直接写命令名（如 'python'、'mkdir'）；"
                    "多动词命令写两段（如 'git push'、'npm install'、'docker build'）。"
                    "list 模式可省略。"
                ),
                required=False,
            ),
            ToolParameter(
                name="scope",
                type="string",
                description=(
                    "cwd / global / session 之一，默认 cwd。"
                    "cwd = 仅在当前工作目录有效（持久化到项目级 .cbagent/permissions.json）；"
                    "global = 所有目录（持久化到项目级文件，跨 cwd 命中）；"
                    "session = 仅本次进程内（不写盘）。"
                ),
                required=False,
                default="cwd",
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        action = parameters.get("action")
        if action not in ("list", "grant", "revoke", "check"):
            return False
        if action in ("grant", "revoke", "check"):
            prefix = parameters.get("prefix")
            if not prefix or not isinstance(prefix, str) or not prefix.strip():
                return False
        scope = parameters.get("scope", "cwd")
        if scope not in ("cwd", "global", "session"):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps(
                {"error": "参数验证失败：action 必须是 list/grant/revoke/check，"
                          "且 grant/revoke/check 需要非空 prefix"},
                ensure_ascii=False,
            )

        action = parameters["action"]
        if action == "list":
            return self._list()

        prefix = parameters["prefix"].strip().lower()
        scope = parameters.get("scope", "cwd")

        if action == "grant":
            return self._grant(prefix, scope)
        if action == "revoke":
            return self._revoke(prefix, scope)
        if action == "check":
            return self._check(prefix)
        return json.dumps({"error": f"未知 action: {action}"}, ensure_ascii=False)

    # ---------- impl ----------

    def _list(self) -> str:
        rules = self._gate.store.list_rules()
        return json.dumps(
            {
                "rules": [r.to_dict() for r in rules],
                "count": len(rules),
            },
            ensure_ascii=False,
        )

    def _grant(self, prefix: str, scope: str) -> str:
        if prefix in DENY_PREFIXES:
            return json.dumps(
                {
                    "error": f"前缀 '{prefix}' 属于高危命令，禁止加入 allowlist。"
                             f"请让用户在弹窗时手动选择。",
                    "denied": True,
                },
                ensure_ascii=False,
            )
        cwd = get_session().cwd
        rule = self._gate.store.add_rule(
            prefix=prefix,
            scope=scope,  # type: ignore[arg-type]
            cwd=cwd if scope == "cwd" else "",
        )
        return json.dumps(
            {
                "ok": True,
                "rule": rule.to_dict(),
                "message": (
                    f"已授权 '{prefix}' "
                    + ("在当前目录" if scope == "cwd"
                       else "在所有目录" if scope == "global"
                       else "在本次会话")
                    + "。后续匹配该前缀的命令将不再弹窗。"
                ),
            },
            ensure_ascii=False,
        )

    def _revoke(self, prefix: str, scope: str) -> str:
        cwd = get_session().cwd if scope == "cwd" else ""
        removed = self._gate.store.remove_rule(
            prefix=prefix,
            scope=scope,  # type: ignore[arg-type]
            cwd=cwd,
        )
        return json.dumps(
            {
                "ok": True,
                "removed_count": len(removed),
                "removed": [r.to_dict() for r in removed],
                "message": (
                    f"已撤销 {len(removed)} 条 '{prefix}' 的授权规则"
                    if removed else
                    f"未找到匹配 '{prefix}' (scope={scope}) 的规则"
                ),
            },
            ensure_ascii=False,
        )

    def _check(self, prefix: str) -> str:
        cwd = get_session().cwd
        rule = self._gate.store.is_allowed(prefix, cwd)
        return json.dumps(
            {
                "prefix": prefix,
                "cwd": cwd,
                "allowed": rule is not None,
                "matched_rule": rule.to_dict() if rule else None,
            },
            ensure_ascii=False,
        )
