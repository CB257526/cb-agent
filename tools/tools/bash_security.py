"""BashTool 安全检测

参考 Claude Code bashSecurity.ts + destructiveCommandWarning.ts。

提供两类能力：
1. 命令切片（parse_pipeline）：用 bashlex AST 把命令切成 simple command 列表，
   每个 simple command 对应一个 argv，配套环境变量赋值前缀剥离。
   解析失败（PowerShell 语法、heredoc 复杂引号嵌套等）→ 降级到 shlex+正则切。
2. 黑名单扫描（check_fatal / check_warnings）：对每段 argv 独立扫描；
   也对原始命令字符串做整体正则扫描，覆盖 AST 抓不到的注入向量
   （例如 `=cmd` Zsh 扩展、子 shell 包裹）。

不负责：权限规则匹配（见 bash_permission.py）、沙箱、退出码语义。
"""

from __future__ import annotations

import re
import shlex
from typing import List, Optional, Tuple

try:
    import bashlex
    _HAS_BASHLEX = True
except ImportError:
    _HAS_BASHLEX = False


# ========== 致命级 — 命中直接拒绝 ==========
# 同时扫描：
#   (a) 原始命令字符串（捕获注入向量、子 shell 包裹）
#   (b) 切出的每段 argv 的 join 形式（捕获被前缀绕过的命令）

FATAL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    # —— 文件系统毁灭性操作 ——
    (re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*f\s+/(?:\s|$)"), "递归强制删除根目录"),
    (re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*f\s+/\S"), "递归强制删除根目录子树"),
    (re.compile(r"\bmkfs\."), "禁止格式化文件系统"),
    (re.compile(r"\bdd\s+if=\S+\s+of=/dev/"), "禁止直接写块设备"),
    (re.compile(r">\s*/dev/sd[a-z]"), "禁止覆写磁盘设备"),
    (re.compile(r"\bchmod\s+-R\s+777\s+/(?:\s|$)"), "禁止递归 777 根目录"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\};:"), "fork 炸弹"),
    (re.compile(r">\s*/etc/(passwd|shadow|sudoers|hosts)\b"), "禁止覆写关键系统文件"),

    # —— 远程脚本管道到 shell ——
    (re.compile(r"\b(curl|wget|fetch)\s+\S+.*\|\s*(?:ba|z|k|d|fi)?sh\b"),
     "下载脚本管道到 shell — 不信任远程代码"),
    (re.compile(r"\$\(\s*(?:curl|wget)\s+[^)]*\|\s*(?:ba|z|k|d|fi)?sh"),
     "命令替换中下载脚本管道到 shell"),

    # —— PowerShell 高危 ——
    (re.compile(r"\bIEX\s*\(", re.IGNORECASE), "PowerShell Invoke-Expression"),
    (re.compile(r"\bInvoke-Expression\b", re.IGNORECASE), "PowerShell Invoke-Expression"),
    (re.compile(r"\b(?:iwr|Invoke-WebRequest)\b[^|]*\|\s*(?:iex|Invoke-Expression)\b",
                re.IGNORECASE),
     "PowerShell IWR 管道到 IEX — 远程脚本执行"),

    # —— Zsh =cmd 扩展（绕过命令白名单）——
    (re.compile(r"(?:^|[\s;&|])=[a-zA-Z_]"), "Zsh =cmd 扩展（命令白名单绕过）"),

    # —— 子 shell 包裹危险命令 ——
    (re.compile(r"\(\s*rm\s+-[a-zA-Z]*r[a-zA-Z]*f\s+/"), "子 shell 包裹的根目录删除"),

    # —— PowerShell / cmd 毁灭性删除根盘 ——
    # Remove-Item / ri / rm（PS 别名）-Recurse -Force 指向根盘或盘符根
    (re.compile(
        r"\b(?:Remove-Item|ri|rm|del|erase)\b"
        r"(?:\s+-[A-Za-z]+)*"
        r"\s+(?:[\"']?[A-Za-z]:[\\/][\"']?\s*$|"
        r"[\"']?[A-Za-z]:[\\/][\"']?\s*[;&|]|"
        r"[\"']?[\\/][\"']?\s*$)",
        re.IGNORECASE,
    ), "禁止删除盘符根目录"),
    # cmd `rd /s /q C:\` / `rmdir /s C:\`
    (re.compile(
        r"\b(?:rd|rmdir)\b\s+/[sq](?:\s+/[sq])*\s+"
        r"[\"']?[A-Za-z]:[\\/]?[\"']?\s*(?:$|[;&|])",
        re.IGNORECASE,
    ), "cmd 强制递归删除盘符根目录"),
    # PowerShell Format-Volume / Clear-Disk
    (re.compile(r"\bFormat-Volume\b", re.IGNORECASE), "PowerShell 格式化卷"),
    (re.compile(r"\bClear-Disk\b", re.IGNORECASE), "PowerShell 清空磁盘"),
]

# ========== 警告级 — 继续执行但加 [警告] ==========

WARNING_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\bsudo\s+"), "sudo 提权操作"),
    (re.compile(r"\bgit\s+push\b.*(?:--force|-f)\b"), "git force push — 会覆写远端历史"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git 硬重置 — 未提交修改将永久丢失"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f"), "git clean -f — 可能删除未跟踪文件"),
    (re.compile(r"\bgit\s+branch\s+-D\b"), "git branch -D — 强制删除分支"),
    (re.compile(r"\bgit\s+commit\b.*--amend"), "git commit --amend — 改写已提交历史"),
    (re.compile(r"\bkubectl\s+delete\b"), "删除 Kubernetes 资源"),
    (re.compile(r"\bterraform\s+destroy\b"), "销毁 Terraform 管理的基础设施"),
    (re.compile(r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE), "删除数据库对象"),
    (re.compile(r"\bTRUNCATE\s+(?:TABLE|DATABASE)?\b", re.IGNORECASE), "截断数据库对象"),
    (re.compile(r"\bDELETE\s+FROM\s+\w+", re.IGNORECASE), "删除数据库记录"),
    # 变量赋值前缀 + 危险动词（绕过 PATH 锁定的常用手段）
    (re.compile(r"^\s*[A-Z_][A-Z0-9_]*=\S+\s+(?:rm|curl|wget|chmod)\b"),
     "环境变量前缀 + 高危命令 — 可能用于绕过 PATH/审计"),

    # —— PowerShell / cmd 删除类（递归 / 强制）——
    # PowerShell：Remove-Item -Recurse / -Force 都视作警告级（路径不在 fatal 范围内）
    (re.compile(r"\b(?:Remove-Item|ri)\b(?:\s+-[A-Za-z]+)*\s+-Recurse\b", re.IGNORECASE),
     "PowerShell 递归删除"),
    (re.compile(r"\b(?:Remove-Item|ri)\b(?:\s+-[A-Za-z]+)*\s+-Force\b", re.IGNORECASE),
     "PowerShell 强制删除（绕过只读/确认）"),
    # cmd：rd /s 或 del /s/q
    (re.compile(r"\b(?:rd|rmdir)\b\s+/[sq]\b", re.IGNORECASE),
     "cmd 递归删除目录"),
    (re.compile(r"\bdel\b(?:\s+/[A-Za-z]+)*\s+/s\b", re.IGNORECASE),
     "cmd 递归删除文件"),
    # PowerShell Stop-Computer / Restart-Computer / Stop-Service
    (re.compile(r"\bStop-Computer\b", re.IGNORECASE), "PowerShell 关机"),
    (re.compile(r"\bRestart-Computer\b", re.IGNORECASE), "PowerShell 重启"),
    # 注册表写入
    (re.compile(r"\b(?:Remove-ItemProperty|Set-ItemProperty)\b\s+[\"']?HK", re.IGNORECASE),
     "PowerShell 修改注册表"),
    (re.compile(r"\breg\b\s+(?:delete|add)\b", re.IGNORECASE),
     "cmd reg 修改注册表"),
]


# ========== AST 切分 ==========


def parse_pipeline(command: str) -> List[List[str]]:
    """把命令切成 [argv1, argv2, ...]，每个 argv 是一个 simple command 的 token 列表。

    优先用 bashlex AST：能正确处理引号、heredoc、命令替换、`a && b | c; d` 嵌套。
    bashlex 解析失败（典型：PowerShell 原生命令）→ 降级用 shlex+正则按 shell
    操作符切分。

    剥离环境变量赋值前缀：`PATH=x rm -rf /` 切出来的 argv = `["rm", "-rf", "/"]`。
    """
    if _HAS_BASHLEX:
        try:
            return _bashlex_split(command)
        except Exception:
            pass
    return _shlex_split(command)


def _bashlex_split(command: str) -> List[List[str]]:
    """用 bashlex AST 遍历，收集所有 'command' 节点的 argv（剥环境变量前缀）。"""
    result: List[List[str]] = []

    def visit(node):
        kind = node.kind
        if kind == "command":
            argv: List[str] = []
            for part in node.parts:
                if part.kind == "word":
                    argv.append(part.word)
                elif part.kind == "assignment":
                    # KEY=value 前缀，跳过；只采集真正的命令 argv
                    continue
                # redirection / parameter / others 不进 argv
            if argv:
                result.append(argv)
        # 递归处理 list / pipeline / compound 等容器节点
        for child in getattr(node, "parts", []) or []:
            if child is not node:
                visit(child)

    trees = bashlex.parse(command)
    for tree in trees:
        visit(tree)
    return result


_OPERATOR_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|(?!\|))\s*")


def _shlex_split(command: str) -> List[List[str]]:
    """降级路径：按 && || ; | 切，每段 shlex 解析，剥环境变量前缀。"""
    segments: List[List[str]] = []
    for raw in _OPERATOR_SPLIT.split(command):
        raw = raw.strip()
        if not raw:
            continue
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError:
            tokens = raw.split()
        # 剥环境变量前缀 KEY=val
        while tokens and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", tokens[0]):
            tokens.pop(0)
        if tokens:
            segments.append(tokens)
    return segments


# ========== 黑名单扫描 ==========


def check_fatal(command: str) -> Optional[str]:
    """致命模式扫描，命中返回 `[拒绝] <原因>`；否则 None。

    扫描两层：
    1. 原始 command 字符串：覆盖注入向量（=cmd / 子 shell / 命令替换）
    2. 每段 argv 拼回的字符串：覆盖被环境变量前缀绕过的命令
    """
    for pattern, reason in FATAL_PATTERNS:
        if pattern.search(command):
            return f"[拒绝] {reason}"

    for argv in parse_pipeline(command):
        joined = " ".join(argv)
        for pattern, reason in FATAL_PATTERNS:
            if pattern.search(joined):
                return f"[拒绝] {reason}"

    return None


def check_warnings(command: str) -> List[str]:
    """警告模式扫描，返回去重后的警告消息列表。"""
    warnings: List[str] = []
    seen: set = set()

    def _scan(text: str):
        for pattern, reason in WARNING_PATTERNS:
            if pattern.search(text) and reason not in seen:
                warnings.append(f"[警告] {reason}")
                seen.add(reason)

    _scan(command)
    for argv in parse_pipeline(command):
        _scan(" ".join(argv))

    return warnings
