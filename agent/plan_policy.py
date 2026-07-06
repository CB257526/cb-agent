"""Plan Mode 服务端工具执行策略。

在 Plan Mode 下，除了通过 prompt 告知模型"请勿写入/修改"，还在 ToolExecutor 层
做硬性拦截。PlanExecutionPolicy 在 _run_one 每个工具执行前被调用 check()，
被拒绝的工具不会调用真正的 runner，直接返回 denied_payload。

两层防护：
1. Prompt 层：_plan_context_text() 注入 Plan Mode 行为指令
2. 执行层：PlanExecutionPolicy 做服务端硬拒绝（本模块）

工具分类：
- PLAN_READ_TOOLS: 始终允许的只读工具（file_read, glob, grep, search 等）
- PLAN_READ_ACTIONS: 仅允许特定 action 的工具（memory.search, rag.search 等）
- bash: 特殊处理 —— 允许只读命令（ls, cat, git log 等），拒绝写入/网络/后台命令
- 其它工具：一律拒绝
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, Optional, Tuple

from tools.tools.bash_permission import extract_prefix
from tools.tools.bash_security import check_fatal, parse_pipeline


PLAN_READ_TOOLS = {
    "ask_user_question",
    "file_read",
    "glob",
    "grep",
    "knowledge_search",
    "list_tools",
    "load_image",
    "ls",
    "my_advanced_search",
    "search",
}

PLAN_READ_ACTIONS = {
    "bash_permission": {"list", "check"},
    "memory": {"search", "stats", "summary"},
    "rag": {"ask", "search", "search_images", "search_audio", "stats"},
}

READONLY_BASH_PREFIXES = {
    "ag",
    "cat",
    "cmp",
    "cut",
    "date",
    "df",
    "diff",
    "dir",
    "du",
    "echo",
    "egrep",
    "fd",
    "fgrep",
    "file",
    "find",
    "gc",
    "gci",
    "get-childitem",
    "get-content",
    "get-date",
    "get-item",
    "get-location",
    "get-process",
    "get-service",
    "git blame",
    "git branch",
    "git describe",
    "git diff",
    "git log",
    "git ls-files",
    "git ls-tree",
    "git remote",
    "git rev-parse",
    "git show",
    "git status",
    "git tag",
    "grep",
    "head",
    "ls",
    "measure-object",
    "more",
    "nl",
    "printf",
    "ps",
    "pwd",
    "rg",
    "select-string",
    "sort",
    "stat",
    "tail",
    "test-path",
    "tree",
    "type",
    "uniq",
    "wc",
    "where",
    "where-object",
    "which",
    "whoami",
}

RAW_DENY_PATTERNS = [
    (re.compile(r"(^|[^<])>{1,2}"), "shell output redirection writes files"),
    (re.compile(r"\b\d?>\s*\S+"), "shell output redirection writes files"),
    (re.compile(r"\b(?:tee|xargs)\b", re.IGNORECASE), "command can write or execute follow-up commands"),
    (re.compile(r"<<"), "heredocs are not allowed in plan mode"),
    (re.compile(r"`|\$\("), "command substitution is not allowed in plan mode"),
    (re.compile(r"\b(?:curl|wget|iwr|Invoke-WebRequest)\b", re.IGNORECASE), "network shell commands are not allowed in plan mode"),
    (re.compile(r"\b(?:Start-Job|Start-ThreadJob|Start-Process|nohup|disown|setsid)\b", re.IGNORECASE), "background process commands are not allowed in plan mode"),
    (re.compile(r"\b(?:rm|del|erase|Remove-Item|ri|mv|move|Move-Item|cp|copy|Copy-Item|touch|mkdir|New-Item|chmod|chown)\b", re.IGNORECASE), "mutating filesystem command"),
    (re.compile(r"\b(?:Set-Content|Add-Content|Out-File|Set-Item|Set-ItemProperty|Remove-ItemProperty)\b", re.IGNORECASE), "mutating PowerShell command"),
]


def denied_payload(tool_name: str, arguments: Dict[str, Any], reason: str) -> str:
    payload = {
        "plan_mode_denied": True,
        "tool": tool_name,
        "reason": reason,
        "hint": "Plan Mode only allows non-mutating exploration. Switch to execute mode after plan approval.",
    }
    if tool_name == "bash":
        payload["command"] = str((arguments or {}).get("command") or "")[:500]
    return json.dumps(payload, ensure_ascii=False)


def is_plan_readonly_bash(command: str, *, run_in_background: bool = False) -> Tuple[bool, str]:
    """检查 bash 命令在 Plan Mode 下是否安全（只读、无副作用）。

    检查链（按顺序，任一步失败即拒绝）：
    1. 空命令 / 后台运行 → 拒绝
    2. check_fatal() 致命模式（rm -rf、fork bomb 等）→ 拒绝
    3. 裸 & 后台操作符（区分于 &&）→ 拒绝
    4. RAW_DENY_PATTERNS 正则匹配（重定向、curl、文件增删等）→ 拒绝
    5. 解析管道，逐个 argv 做 _is_readonly_argv() 检查
       - 命令前缀不在 READONLY_BASH_PREFIXES → 拒绝
       - find/fd/git/sort/rg 做额外的危险 flag 检查
    """
    command = str(command or "").strip()
    if not command:
        return False, "empty bash command"
    if run_in_background:
        return False, "background bash commands are not allowed in plan mode"

    fatal = check_fatal(command)
    if fatal:
        return False, fatal
    if _has_unquoted_single_ampersand(command):
        return False, "background/control operators are not allowed in plan mode"
    for pattern, reason in RAW_DENY_PATTERNS:
        if pattern.search(command):
            return False, reason

    segments = parse_pipeline(command)
    if not segments:
        return False, "could not parse bash command"
    for argv in segments:
        ok, reason = _is_readonly_argv(argv)
        if not ok:
            return False, reason
    return True, ""


def _has_unquoted_single_ampersand(command: str) -> bool:
    quote: Optional[str] = None
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
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "&":
            prev_char = command[index - 1] if index > 0 else ""
            next_char = command[index + 1] if index + 1 < len(command) else ""
            if prev_char != "&" and next_char != "&":
                return True
    return False


def _is_readonly_argv(argv: Iterable[str]) -> Tuple[bool, str]:
    tokens = [str(x) for x in argv if str(x)]
    if not tokens:
        return False, "empty command segment"
    prefix = extract_prefix(tokens).lower()
    if prefix not in READONLY_BASH_PREFIXES:
        return False, f'bash command "{prefix or tokens[0]}" is not read-only in plan mode'

    head = tokens[0].split("/")[-1].split("\\")[-1].lower()
    if head == "find":
        dangerous = {"-exec", "-execdir", "-delete", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf"}
        if any(t in dangerous for t in tokens[1:]):
            return False, "find write/exec flags are not allowed in plan mode"
    if head == "fd" and any(t in {"-x", "--exec", "-X", "--exec-batch"} for t in tokens[1:]):
        return False, "fd exec flags are not allowed in plan mode"
    if head == "git":
        return _is_readonly_git(tokens)
    if head in {"sort", "uniq"} and _has_option_value(tokens[1:], {"-o", "--output"}):
        return False, f"{head} output-file flags are not allowed in plan mode"
    if head in {"rg", "grep", "ag", "egrep", "fgrep"}:
        if _has_option_value(tokens[1:], {"--output"}):
            return False, "search output-file flags are not allowed in plan mode"
    return True, ""


def _has_option_value(tokens: list[str], option_names: set[str]) -> bool:
    for index, token in enumerate(tokens):
        if token in option_names:
            return index + 1 < len(tokens)
        if any(token.startswith(name + "=") for name in option_names):
            return True
    return False


def _is_readonly_git(tokens: list[str]) -> Tuple[bool, str]:
    if len(tokens) < 2:
        return False, "git requires an explicit read-only subcommand in plan mode"
    sub = tokens[1].lower()
    always_read = {"status", "log", "diff", "show", "blame", "rev-parse", "ls-files", "ls-tree", "describe"}
    if sub in always_read:
        if _has_option_value(tokens[2:], {"--output"}):
            return False, f"git {sub} output-file flags are not allowed in plan mode"
        return True, ""
    if sub == "branch":
        dangerous = {"-d", "-D", "-m", "-M", "--delete", "--move", "--copy", "-c", "-C"}
        if any(t in dangerous or t.startswith("--set-upstream") for t in tokens[2:]):
            return False, "git branch mutation flags are not allowed in plan mode"
        non_flags = [t for t in tokens[2:] if not t.startswith("-")]
        if non_flags:
            return False, "git branch creation/rename is not allowed in plan mode"
        return True, ""
    if sub == "tag":
        dangerous = {"-d", "--delete", "-f", "--force", "-a", "-s", "-u", "-m", "-F"}
        if any(t in dangerous for t in tokens[2:]):
            return False, "git tag mutation flags are not allowed in plan mode"
        non_flags = [t for t in tokens[2:] if not t.startswith("-")]
        if non_flags:
            return False, "git tag creation is not allowed in plan mode"
        return True, ""
    if sub == "remote":
        if len(tokens) == 2 or tokens[2] in {"-v", "show", "get-url"}:
            return True, ""
        return False, "git remote mutation subcommands are not allowed in plan mode"
    return False, f'git {sub} is not read-only in plan mode'


class PlanExecutionPolicy:
    """ToolExecutor 在每个工具调用前使用的可调用策略对象。

    两个方法对应 executor 的两个调用点：
    - check(name, args) → (allowed: bool, reason: str|None): 判断是否允许执行
    - denied_result(name, args, reason) → str: 生成拒绝时的 JSON 结果

    三层判断逻辑（check 内）：
    1. bash → is_plan_readonly_bash() 细粒度检查
    2. name in PLAN_READ_TOOLS → 直接允许
    3. name in PLAN_READ_ACTIONS → 仅允许白名单中的 action
    4. 其它 → 拒绝
    """

    mode = "plan"

    def check(self, tool_name: str, arguments: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        args = arguments or {}
        if tool_name == "bash":
            ok, reason = is_plan_readonly_bash(
                str(args.get("command") or ""),
                run_in_background=bool(args.get("run_in_background")),
            )
            return ok, None if ok else reason
        if tool_name in PLAN_READ_TOOLS:
            return True, None
        if tool_name in PLAN_READ_ACTIONS:
            action = str(args.get("action") or "")
            if action in PLAN_READ_ACTIONS[tool_name]:
                return True, None
            return False, f'{tool_name} action "{action}" is not allowed in plan mode'
        return False, f'{tool_name} is not allowed in plan mode'

    def denied_result(self, tool_name: str, arguments: Dict[str, Any], reason: str) -> str:
        return denied_payload(tool_name, arguments, reason)


__all__ = [
    "PlanExecutionPolicy",
    "denied_payload",
    "is_plan_readonly_bash",
]
