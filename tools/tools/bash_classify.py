"""命令分类

参考 Claude Code utils.ts 中 isSearchOrReadBashCommand / isSilentBashCommand 的逻辑。

仅负责把命令归类成 search / read / list / silent / normal，
分类结果用于：
- REPL 渲染时是否折叠输出
- 退出码语义解释的兜底
- 未来按类型做差异化日志/计费

不包含执行、安全检测、shell 选择等任何副作用。
"""

from typing import Dict, Set


# ========== 命令分类集 ==========

SEARCH_COMMANDS: Set[str] = {"find", "grep", "rg", "ag", "locate", "which", "whereis"}
READ_COMMANDS: Set[str] = {
    "cat", "head", "tail", "wc", "stat", "file",
    "jq", "awk", "cut", "sort", "uniq", "tr", "strings",
}
LIST_COMMANDS: Set[str] = {"ls", "tree", "du", "dir"}
SILENT_COMMANDS: Set[str] = {
    "mv", "cp", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp",
    "touch", "ln", "cd", "export", "unset", "wait",
}

# 退出码语义中性命令（成功/失败均无副作用）
SEMANTIC_NEUTRAL_COMMANDS: Set[str] = {"echo", "printf", "true", "false", ":"}


def classify_command(command: str) -> Dict[str, str]:
    """解析命令类型：search / read / list / silent / normal。

    只看第一个非控制 token、非环境变量赋值前缀的 token。
    复合命令（&&、|| 等）以第一个真实命令为准。
    """
    tokens = command.strip().split()
    if not tokens:
        return {"kind": "normal"}
    for token in tokens:
        # 跳过 shell 控制符
        if token in ("&&", "||", "|", ";", ">", ">>", "<"):
            continue
        # 跳过形如 KEY=value 的环境变量赋值前缀
        if "=" in token and not token.startswith("-"):
            continue
        base = token.split("/")[-1]
        if base in SEARCH_COMMANDS:
            return {"kind": "search"}
        if base in READ_COMMANDS:
            return {"kind": "read"}
        if base in LIST_COMMANDS:
            return {"kind": "list"}
        if base in SILENT_COMMANDS:
            return {"kind": "silent"}
        return {"kind": "normal"}
    return {"kind": "normal"}
