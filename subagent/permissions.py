"""子代理服务端权限策略。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

from agent.plan_policy import is_plan_readonly_bash
from subagent.models import SubagentDefinition
from tools.tools.bash_security import parse_pipeline


WRITE_TOOLS = {
    "file_edit",
    "file_write",
    "knowledge_write",
    "memory_store",
}

ALWAYS_DENIED_TOOLS = {
    "agent",
    "agent_task",
    "ask_user_question",
    "bash_permission",
    "qqtool",
    "wechattool",
    "send_message_asset",
}

PATH_ARGUMENT_KEYS = {
    "path",
    "file_path",
    "filepath",
    "cwd",
    "directory",
    "target_path",
    "output_path",
}

SENSITIVE_RUNTIME_DIRS = (
    ".git",
    ".cbagent",
)

PUBLIC_INTERNAL_DIRS = (
    ".cbagent/agents",
    ".cbagent/skills",
)

SENSITIVE_RUNTIME_FILES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
}

_DYNAMIC_SHELL_PATH_PATTERN = re.compile(r"`|\$\(|<\(|>\(|<<")
_DETACHED_PROCESS_PATTERN = re.compile(
    r"\b(?:nohup|disown|setsid|start-job|start-threadjob|start-process)\b",
    re.IGNORECASE,
)
_SHELL_ASSIGNMENT_PATTERN = re.compile(
    r"(?:^|[\s;&|])(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*=([^\s;&|]+)"
)
_SHELL_REDIRECTION_PATTERN = re.compile(
    r"(?:\d*>>?|<)\s*([^\s;&|]+)"
)
_EMBEDDED_POSIX_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.:-])((?:~(?:[A-Za-z0-9_.-]+)?/|(?:\.\.?/)+|/)"
    r"[^\s'\"`,;|&()<>]+)"
)
_EMBEDDED_WINDOWS_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z]:[\\/][^\s'\"`,;|&()<>]+)"
)
_EXTERNAL_PATH_VARIABLE_PATTERN = re.compile(
    r"\$(?:\{)?(?:HOME|TMPDIR|OLDPWD|XDG_[A-Za-z0-9_]+)(?:\})?",
    re.IGNORECASE,
)


class SubagentExecutionPolicy:
    """在 ToolExecutor 层强制角色权限和工作区边界。"""

    def __init__(
        self,
        definition: SubagentDefinition,
        workspace_dir: Path,
        *,
        allowed_internal_paths: Iterable[Path] = (),
    ) -> None:
        self.definition = definition
        self.workspace_dir = Path(workspace_dir).resolve()
        self.allowed_internal_paths = tuple(
            [
                (self.workspace_dir / relative).resolve()
                for relative in PUBLIC_INTERNAL_DIRS
            ]
            + [Path(path).resolve() for path in allowed_internal_paths]
        )
        self.sensitive_runtime_dirs = tuple(
            (self.workspace_dir / relative).resolve()
            for relative in SENSITIVE_RUNTIME_DIRS
        )
        self.has_explicit_tool_filter = definition.tools is not None
        self.allowed_tools = set(definition.tools or ())
        self.denied_tools = set(definition.permissions.denied_tools) | ALWAYS_DENIED_TOOLS

    def check(self, tool_name: str, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        name = str(tool_name or "").strip()
        args = arguments if isinstance(arguments, dict) else {}
        if name in self.denied_tools:
            return False, f"角色 {self.definition.name} 禁止使用工具 {name}"
        if self.has_explicit_tool_filter and name not in self.allowed_tools:
            return False, f"工具 {name} 不在角色 {self.definition.name} 的允许列表中"
        if name in WRITE_TOOLS and not self.definition.permissions.workspace_write:
            return False, f"角色 {self.definition.name} 是只读角色"

        path_ok, path_reason = self._check_paths(args, mutating=name in WRITE_TOOLS)
        if not path_ok:
            return False, path_reason

        if name == "bash":
            return self._check_bash(args)
        return True, ""

    def denied_result(self, tool_name: str, arguments: Dict[str, Any], reason: str) -> str:
        payload = {
            "subagent_permission_denied": True,
            "subagent_type": self.definition.name,
            "tool": tool_name,
            "reason": reason,
            "hint": "该操作超出当前子代理角色权限，请由主 Agent 处理或改用合适角色。",
        }
        if tool_name == "bash":
            payload["command"] = str((arguments or {}).get("command") or "")[:500]
        return json.dumps(payload, ensure_ascii=False)

    def _check_bash(self, arguments: Dict[str, Any]) -> Tuple[bool, str]:
        mode = self.definition.permissions.bash_mode
        if mode == "deny":
            return False, f"角色 {self.definition.name} 不允许执行 Bash"
        if bool(arguments.get("run_in_background", False)):
            return False, "子代理不允许启动脱离任务生命周期的后台 Bash"
        command = str(arguments.get("command") or "")
        if _DETACHED_PROCESS_PATTERN.search(command) or _has_unquoted_single_ampersand(command):
            return False, "子代理不允许启动无法随任务取消的脱离进程"
        for argv in parse_pipeline(command):
            executable = Path(str(argv[0] or "")).name.lower() if argv else ""
            if executable in {"find", "tree"}:
                return False, "子代理 Bash 不允许递归遍历整个工作区，请使用 glob 或 ls"
            if executable in {"rg", "grep", "ag", "egrep", "fgrep"} and any(
                str(token).lower() in {"--hidden", "--no-ignore", "-u", "-uu", "-uuu"}
                for token in argv[1:]
            ):
                return False, "子代理 Bash 搜索不允许绕过隐藏目录忽略规则，请使用 grep 工具"
        path_ok, path_reason = self._check_bash_paths(command)
        if not path_ok:
            return False, path_reason
        if mode == "read_only":
            return is_plan_readonly_bash(
                str(arguments.get("command") or ""),
                run_in_background=bool(arguments.get("run_in_background", False)),
            )
        if not self.definition.permissions.workspace_write:
            # inherit 只继承父会话的命令授权，不应绕过角色自己的只读标记。
            return is_plan_readonly_bash(command, run_in_background=False)
        # inherit 只表示角色层允许，实际 BashTool 仍必须经过父会话 PermissionGate。
        return True, ""

    def _check_bash_paths(self, command: str) -> Tuple[bool, str]:
        """保守拦截 Bash 参数中显式出现的工作区外路径。"""

        if self.definition.permissions.external_paths:
            return True, ""
        if _DYNAMIC_SHELL_PATH_PATTERN.search(command):
            return False, "子代理 Bash 不允许使用无法静态校验路径的命令替换或进程替换"

        # bashlex 在 POSIX 环境会把 Windows 路径中的反斜杠当转义符剥掉，
        # 因此盘符路径必须先从原始命令扫描，不能只依赖 argv。
        for match in _EMBEDDED_WINDOWS_PATH_PATTERN.finditer(command):
            allowed, reason = self._check_bash_path_token(match.group(1))
            if not allowed:
                return False, reason

        for argv in parse_pipeline(command):
            # argv[0] 是可执行程序；其余 token 中只有明显的路径形态参与检查。
            for token in argv[1:]:
                allowed, reason = self._check_bash_path_token(str(token or ""))
                if not allowed:
                    return False, reason

        # bashlex 的 argv 不包含重定向节点，也会剥掉环境变量赋值前缀；这里从原始
        # 命令补扫这两类位置，防止 `>/tmp/x` 和 `OUT=/tmp/x` 绕过工作区边界。
        for pattern in (_SHELL_REDIRECTION_PATTERN, _SHELL_ASSIGNMENT_PATTERN):
            for match in pattern.finditer(command):
                allowed, reason = self._check_bash_path_token(match.group(1))
                if not allowed:
                    return False, reason
        return True, ""

    def _check_bash_path_token(self, token: str) -> Tuple[bool, str]:
        """校验一个 Bash token 中的纯路径、选项路径和代码字符串内嵌路径。"""

        value = str(token or "").strip().strip("'\"")
        if not value or value in {"/dev/null", "&1", "&2"} or "://" in value:
            return True, ""
        if _EXTERNAL_PATH_VARIABLE_PATTERN.search(value):
            return False, f"子代理 Bash 不允许使用工作区外路径变量: {value}"

        # $PWD 明确绑定到子代理工作区，可以静态替换后继续做 relative_to 校验；
        # 其它带目录分隔符的变量无法可靠求值，保守拒绝。
        normalized = value.replace("${PWD}", str(self.workspace_dir)).replace(
            "$PWD", str(self.workspace_dir)
        )
        if normalized.startswith("$"):
            return False, f"子代理 Bash 路径包含无法解析的变量: {value}"
        if "$" in normalized and ("/" in normalized or "\\" in normalized):
            return False, f"子代理 Bash 路径包含无法解析的变量: {value}"

        candidates = [match.group(1) for match in _EMBEDDED_POSIX_PATH_PATTERN.finditer(normalized)]
        windows_candidates = [
            match.group(1) for match in _EMBEDDED_WINDOWS_PATH_PATTERN.finditer(normalized)
        ]
        if windows_candidates:
            if os.name != "nt":
                return False, f"子代理 Bash 不允许访问工作区外路径: {windows_candidates[0]}"
            candidates.extend(windows_candidates)

        option_value = normalized.split("=", 1)[1] if normalized.startswith("-") and "=" in normalized else normalized
        if option_value.startswith(".") and any(char in option_value for char in "*?["):
            return False, f"子代理 Bash 不允许用隐藏路径通配符访问私有文件: {value}"
        if (
            Path(option_value).name in SENSITIVE_RUNTIME_FILES
            or _is_sensitive_env_file(Path(option_value).name)
        ):
            candidates.append(option_value)
        if not candidates and (
            option_value.startswith(("/", "~", "."))
            or "/" in option_value
            or "\\" in option_value
        ):
            candidates.append(option_value)
        elif not candidates and not option_value.startswith("-"):
            # 普通文件名也可能是指向工作区外的符号链接；把所有非选项参数按
            # “可能是路径”解析不会影响普通关键词，因为不存在的相对路径仍位于工作区。
            candidates.append(option_value)

        for candidate in candidates:
            cleaned = candidate.rstrip(":]")
            if cleaned == "/dev/null":
                continue
            path = Path(cleaned).expanduser()
            if not path.is_absolute():
                path = self.workspace_dir / path
            allowed, reason = self._check_resolved_path(
                path.resolve(strict=False),
                original_value=candidate,
                mutating=False,
            )
            if not allowed:
                return False, reason
        return True, ""

    def _check_paths(self, arguments: Dict[str, Any], *, mutating: bool) -> Tuple[bool, str]:
        if self.definition.permissions.external_paths:
            return True, ""
        for value in _iter_path_values(arguments):
            path = Path(value).expanduser()
            if not path.is_absolute():
                path = self.workspace_dir / path
            allowed, reason = self._check_resolved_path(
                path.resolve(strict=False),
                original_value=value,
                mutating=mutating,
            )
            if not allowed:
                return False, reason
        return True, ""

    def _check_resolved_path(
        self,
        resolved: Path,
        *,
        original_value: str,
        mutating: bool,
    ) -> Tuple[bool, str]:
        """同时校验工作区边界和 cb-agent 私有运行目录。"""

        action = "写入" if mutating else "访问"
        try:
            relative = resolved.relative_to(self.workspace_dir)
        except (OSError, ValueError):
            return False, f"子代理不允许{action}工作区外路径: {original_value}"

        for allowed_root in self.allowed_internal_paths:
            try:
                resolved.relative_to(allowed_root)
                return True, ""
            except (OSError, ValueError):
                continue

        for private_root in self.sensitive_runtime_dirs:
            try:
                resolved.relative_to(private_root)
                return False, f"子代理不允许{action}其它会话或运行时私有路径: {original_value}"
            except (OSError, ValueError):
                continue

        relative_text = relative.as_posix()
        if (
            relative_text in SENSITIVE_RUNTIME_FILES
            or relative.name in SENSITIVE_RUNTIME_FILES
            or _is_sensitive_env_file(relative.name)
        ):
            return False, f"子代理不允许{action}凭据或权限配置文件: {original_value}"
        return True, ""


def _iter_path_values(arguments: Dict[str, Any]) -> Iterable[str]:
    for key, value in arguments.items():
        if key not in PATH_ARGUMENT_KEYS or value is None:
            continue
        if isinstance(value, str) and value.strip():
            yield value.strip()
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    yield item.strip()


def _has_unquoted_single_ampersand(command: str) -> bool:
    """识别 shell 脱离执行符号 `&`，但允许作为串行条件的 `&&`。"""

    quote = ""
    escaped = False
    for index, char in enumerate(command):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "&":
            previous = command[index - 1] if index > 0 else ""
            following = command[index + 1] if index + 1 < len(command) else ""
            if previous in {">", "<", "|"} or following == ">":
                continue
            if previous != "&" and following != "&":
                return True
    return False


def _is_sensitive_env_file(name: str) -> bool:
    """识别可能含真实凭据的 .env 文件，同时允许 example/sample/template。"""

    lowered = str(name or "").lower()
    if lowered == ".env":
        return True
    if not lowered.startswith(".env."):
        return False
    return not lowered.endswith((".example", ".sample", ".template"))


__all__ = [
    "ALWAYS_DENIED_TOOLS",
    "PUBLIC_INTERNAL_DIRS",
    "SENSITIVE_RUNTIME_DIRS",
    "SubagentExecutionPolicy",
    "WRITE_TOOLS",
]
