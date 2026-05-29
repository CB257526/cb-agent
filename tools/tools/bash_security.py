"""BashTool 安全检测

命令语句层面的危险模式检测。
参考 Claude Code bashSecurity.ts + destructiveCommandWarning.ts 的设计。

负责：致命拦截（直接拒绝）+ 警告（继续执行但加提示）。
不负责：AST 级语法分析、权限规则匹配（cb-agent 不需要）。
"""

import re
from typing import List, Optional

# ========== 致命级 — 命中直接拒绝，不给模型任何执行机会 ==========

FATAL_PATTERNS: list = [
    (re.compile(r"rm\s+-rf\s+/", re.IGNORECASE),                 "禁止递归删除根目录"),
    (re.compile(r"(?:^|[;&|])\s*rm\s+-[a-zA-Z]*[rR][a-zA-Z]*f"), "递归强制删除 — 可能造成大规模数据丢失"),
    (re.compile(r"mkfs\."),                                        "禁止格式化文件系统"),
    (re.compile(r"dd\s+if="),                                      "禁止直接操作块设备"),
    (re.compile(r">\s*/dev/sd[a-z]"),                              "禁止覆写磁盘设备"),
    (re.compile(r"chmod\s+777\s+/"),                               "禁止递归修改根目录权限"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"),                    "检测到 fork 炸弹模式"),
    (re.compile(r">\s*/etc/(passwd|shadow|sudoers|hosts)"),       "禁止覆写关键系统文件"),
    (re.compile(r"curl\s+\S+\s*\|\s*(?:ba)?sh"),                   "curl 管道到 shell — 不信任的远程脚本"),
    (re.compile(r"wget\s+\S+\s*\|\s*(?:ba)?sh"),                   "wget 管道到 shell — 不信任的远程脚本"),
]

# ========== 警告级 — 继续执行但在输出前加 [警告] 前缀 ==========

WARNING_PATTERNS: list = [
    (re.compile(r"sudo\s+"),                                       "sudo 提权操作"),
    (re.compile(r"git\s+push\b.*(?:--force|-f)\b"),               "git force push — 会覆写远端历史"),
    (re.compile(r"git\s+reset\s+--hard"),                          "git 硬重置 — 未提交修改将永久丢失"),
    (re.compile(r"git\s+clean\s+-[a-z]*f"),                       "git clean -f — 可能删除未跟踪文件"),
    (re.compile(r"git\s+branch\s+-D\b"),                          "git branch -D — 强制删除分支"),
    (re.compile(r"git\s+commit\b.*--amend"),                       "git commit --amend — 改写已提交历史"),
    (re.compile(r"\bkubectl\s+delete\b"),                          "删除 Kubernetes 资源"),
    (re.compile(r"\bterraform\s+destroy\b"),                       "销毁 Terraform 管理的基础设施"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.I),     "删除数据库对象"),
    (re.compile(r"\bTRUNCATE\s+(?:TABLE|DATABASE)?\b", re.I),     "截断数据库对象，数据不可恢复"),
    (re.compile(r"\bDELETE\s+FROM\s+\w+", re.I),                  "删除数据库记录"),
]


def check_fatal(command: str) -> Optional[str]:
    """扫描致命危险模式，命中返回拒绝原因字符串；否则返回 None。"""
    for pattern, reason in FATAL_PATTERNS:
        if pattern.search(command):
            return f"[拒绝] {reason}"
    return None


def check_warnings(command: str) -> List[str]:
    """扫描警告级危险模式，返回警告消息列表。"""
    warnings: List[str] = []
    for pattern, reason in WARNING_PATTERNS:
        if pattern.search(command):
            warnings.append(f"[警告] {reason}")
    return warnings
