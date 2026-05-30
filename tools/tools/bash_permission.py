"""交互式命令权限模块

参考 Claude Code 的 bashPermissions.ts + readOnlyValidation.ts，但适配
cb-agent 的同步 REPL 架构：

cb-agent 没有前端弹窗，REPL 是同步 input() 阻塞循环，工具调用同样
全程同步（run_agent.py:_chat_once → registry.execute_tool → BashTool.run）。
所以"权限确认"直接在 BashTool.run 内部用 stdin 阻塞实现：
打印多选框、读 input()、解析选择、视情况追加 allowlist。

三档决策（PermissionGate.evaluate）：
- DENY: check_fatal 命中
- ALLOW: 命中 allowlist（按命令前缀 + cwd 维度匹配）
- ASK: check_warnings 命中且未在 allowlist → 弹窗

弹窗选项：
- [1] 允许这一次（不写 allowlist）
- [2] 总是允许 "<prefix>" 在 当前 cwd
- [3] 总是允许 "<prefix>" 在 所有目录
- [4] 拒绝

allowlist 持久化到 ./.cbagent/permissions.json（项目级，跟着 cwd 走）。
非 TTY 环境（管道/重定向 stdin、CI）→ 直接拒绝并标 permission_unavailable。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import List, Literal, Optional


# ========== 前缀提取 ==========

# 多动词命令：取首两个 token 作为前缀（与 Claude Code 行为一致）
MULTI_VERB_COMMANDS = {
    "git", "npm", "pnpm", "yarn", "docker", "kubectl",
    "cargo", "pip", "uv", "pipx", "poetry",
    "python", "python3", "node", "go", "dotnet",
    "gh", "glab",
}


def extract_prefix(argv: List[str]) -> str:
    """从 argv 提取允许列表用的"前缀"。

    规则：
    - 空 argv → 空串
    - 多动词命令（git/npm/...）：取首两个 token，例如 ["git","push","-f"] → "git push"
    - 否则取首 token（去掉路径前缀）：["/usr/bin/curl","-X","POST"] → "curl"
    """
    if not argv:
        return ""
    head = argv[0].split("/")[-1].split("\\")[-1]
    if head in MULTI_VERB_COMMANDS and len(argv) >= 2:
        sub = argv[1]
        if not sub.startswith("-"):
            return f"{head} {sub}"
    return head


# ========== 只读命令白名单 ==========
#
# 默认 strict 模式下，只有命中下表的命令才直接放行，其他一律 ASK。
# 与 Claude Code 的"read/write/grep 等之外都弹窗"对齐：
# - 单 token 命令（POSIX + cmd + PowerShell 三系合并）
# - 多动词命令的只读子命令（git status/log/diff、docker ps/inspect、kubectl get 等）
#
# 命中匹配用 extract_prefix 的输出（小写比对，避免 PowerShell 大小写敏感坑）。

READ_ONLY_PREFIXES: set = {
    # —— 目录切换 / shell 状态：不动文件系统，仅会话态，视为只读 ——
    "cd", "chdir", "pushd", "popd",
    "set-location", "sl",
    # —— POSIX 文件查看 ——
    "ls", "ll", "la", "pwd", "cat", "head", "tail", "less", "more",
    "file", "stat", "wc", "tree", "tac", "nl",
    # —— 搜索 ——
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "find", "fd", "locate", "whereis", "which", "type",
    # —— 信息 / 进程查看 ——
    "echo", "printf", "date", "whoami", "id", "groups",
    "uname", "hostname", "df", "du", "free", "top", "htop",
    "ps", "pgrep", "uptime", "env", "printenv", "history",
    "true", "false",
    # —— diff / 版本 ——
    "diff", "cmp", "md5sum", "sha1sum", "sha256sum",
    # —— cmd.exe 只读 ——
    "dir", "where", "tasklist", "systeminfo", "ver", "vol",
    "ipconfig", "ping", "tracert", "nslookup", "netstat",
    # —— PowerShell 只读 cmdlet（小写）——
    "get-childitem", "gci", "ls",  # ls 是 PS 别名
    "get-content", "gc", "cat",
    "get-location", "gl", "pwd",
    "get-process", "gps", "ps",
    "get-service", "gsv",
    "get-item", "gi",
    "get-itemproperty", "gp",
    "get-date",
    "get-host",
    "select-string", "sls",
    "test-path",
    "measure-object", "measure",
    "where-object", "where", "?",
    "format-table", "ft", "format-list", "fl",
    "out-host", "out-string",
    # —— git 只读子命令 ——
    "git status", "git log", "git diff", "git show",
    "git branch", "git tag", "git remote",
    "git blame", "git rev-parse", "git ls-files", "git ls-tree",
    "git describe", "git reflog", "git stash",
    "git fetch",  # fetch 不改本地工作树，视为只读
    # 注意：git config / kubectl config / npm config 不在只读 — 这些命令带参数
    # 等同写操作（改 ~/.gitconfig 等），让它们走 ASK 弹窗
    # —— docker / kubectl / 包管理只读 ——
    "docker ps", "docker images", "docker inspect", "docker logs",
    "docker version", "docker info", "docker stats",
    "kubectl get", "kubectl describe", "kubectl logs",
    "kubectl version",
    "npm list", "npm view", "npm outdated",
    "pip list", "pip show", "pip freeze",
    "uv pip", "poetry show",
    "cargo metadata", "cargo tree",
    "go list", "go version", "go env",
    # —— gh / glab 只读 ——
    "gh status", "gh auth",
    # 注意：gh pr / gh issue / gh repo 不在只读 — 它们的 create / merge / delete
    # 子命令会改远端状态。让它们走 ASK 弹窗
}


def is_read_only(argv: List[str]) -> bool:
    """argv 是否落在只读白名单内。

    注意：`gh pr` 整个被列入只读，但实际 `gh pr create` 会改远端。这里取舍是
    宁可让 `gh pr <X>` 弹一次窗，也比静默放行 create/merge/close 安全 ——
    所以 strict 模式下"前缀命中只读"也只是 ALLOW 第一层，更细的规则要靠
    用户主动加 allowlist 调整粒度。这里实现成精确前缀匹配，不做"父前缀通配"。
    """
    prefix = extract_prefix(argv).lower()
    return prefix in READ_ONLY_PREFIXES


# ========== 决策类型 ==========


class Decision(Enum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass
class Rule:
    prefix: str  # 命令前缀，如 "git push"
    scope: Literal["session", "cwd", "global"]
    cwd: str = ""  # scope=cwd 时存绝对路径
    added_at: str = ""

    def to_dict(self) -> dict:
        return {
            "prefix": self.prefix,
            "scope": self.scope,
            "cwd": self.cwd,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(
            prefix=d["prefix"],
            scope=d["scope"],
            cwd=d.get("cwd", ""),
            added_at=d.get("added_at", ""),
        )


# ========== 持久化存储 ==========


class PermissionStore:
    """allowlist 的读写。

    项目级文件：./.cbagent/permissions.json （cwd-bound 规则 + global 规则）
    会话级（scope=session）规则只在内存里，进程退出即丢
    """

    def __init__(self, store_path: Optional[Path] = None):
        self._path = Path(store_path or "./.cbagent/permissions.json")
        self._session_rules: List[Rule] = []
        self._lock = threading.Lock()

    def is_allowed(self, prefix: str, cwd: str) -> Optional[Rule]:
        """命中任意规则即允许，返回命中规则；否则 None。"""
        cwd_norm = self._normalize_cwd(cwd)
        for rule in self._all_rules():
            if rule.prefix != prefix:
                continue
            if rule.scope == "global":
                return rule
            if rule.scope == "session":
                return rule
            if rule.scope == "cwd" and self._normalize_cwd(rule.cwd) == cwd_norm:
                return rule
        return None

    def add_rule(
        self,
        prefix: str,
        scope: Literal["session", "cwd", "global"],
        cwd: str = "",
    ) -> Rule:
        rule = Rule(
            prefix=prefix,
            scope=scope,
            cwd=self._normalize_cwd(cwd) if scope == "cwd" else "",
            added_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            if scope == "session":
                self._session_rules.append(rule)
            else:
                rules = self._load_persisted()
                rules.append(rule)
                self._save_persisted(rules)
        return rule

    def remove_rule(
        self,
        prefix: str,
        scope: Optional[Literal["session", "cwd", "global"]] = None,
        cwd: str = "",
    ) -> List[Rule]:
        """删除规则。

        scope=None：删所有同 prefix 的规则（持久化 + session 都清）。
        scope 指定：只删该 scope 下的；cwd 范围还需 cwd 匹配。
        返回被删除的规则列表（方便回显）。
        """
        cwd_norm = self._normalize_cwd(cwd) if cwd else ""
        removed: List[Rule] = []
        with self._lock:
            # session 内存
            keep_session: List[Rule] = []
            for r in self._session_rules:
                if r.prefix != prefix:
                    keep_session.append(r)
                    continue
                if scope is not None and r.scope != scope:
                    keep_session.append(r)
                    continue
                removed.append(r)
            self._session_rules = keep_session

            # 持久化文件
            persisted = self._load_persisted()
            keep_persisted: List[Rule] = []
            for r in persisted:
                if r.prefix != prefix:
                    keep_persisted.append(r)
                    continue
                if scope is not None and r.scope != scope:
                    keep_persisted.append(r)
                    continue
                if scope == "cwd" and cwd_norm and self._normalize_cwd(r.cwd) != cwd_norm:
                    keep_persisted.append(r)
                    continue
                removed.append(r)
            if len(keep_persisted) != len(persisted):
                self._save_persisted(keep_persisted)
        return removed

    def list_rules(self) -> List[Rule]:
        return list(self._all_rules())

    # ---------- 内部 ----------

    def _all_rules(self) -> List[Rule]:
        return list(self._session_rules) + self._load_persisted()

    def _load_persisted(self) -> List[Rule]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return [Rule.from_dict(r) for r in data.get("rules", [])]
        except (json.JSONDecodeError, OSError, KeyError):
            return []

    def _save_persisted(self, rules: List[Rule]) -> None:
        # 只保存 cwd / global，不保存 session
        persisted = [r for r in rules if r.scope in ("cwd", "global")]
        payload = {"rules": [r.to_dict() for r in persisted]}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _normalize_cwd(cwd: str) -> str:
        if not cwd:
            return ""
        return str(Path(cwd).resolve()).rstrip("\\/").lower() if os.name == "nt" else str(Path(cwd).resolve()).rstrip("/")


# ========== 决策门 ==========


@dataclass
class GateResult:
    decision: Decision
    reason: str = ""
    matched_rule: Optional[Rule] = None
    permission_unavailable: bool = False  # 非 TTY 时为 True


class PermissionGate:
    """评估 + 弹窗 + 写 allowlist 的协调者。

    strict 默认开启，语义对齐 Claude Code：
    - 命中 fatal 模式 → 上游已拦，这里不会进 evaluate
    - 命中 warnings → 必 ASK（即使所有段都是只读也要弹）
    - strict=True：所有段都必须命中只读白名单或 allowlist，否则 ASK
    - strict=False（兼容旧行为）：只有 warnings 命中才 ASK
    """

    def __init__(
        self,
        store: Optional[PermissionStore] = None,
        strict: bool = True,
    ):
        self.store = store or PermissionStore()
        self.strict = strict

    def evaluate(
        self,
        command: str,
        segments: List[List[str]],
        warnings: List[str],
        cwd: str,
    ) -> GateResult:
        """决策入口。fatal 由调用方上游处理（不进这里）。

        优先级：
          1. allowlist 命中（任一段命中即整命令放行，跟 Claude Code 行为一致）
             → ALLOW（matched_rule 指向命中规则）
          2. 有 warnings → ASK（无论 strict 模式）
          3. strict 模式下，逐段检查是否都属于只读白名单
             - 全部只读 → ALLOW
             - 任一段不在只读 → ASK
          4. 非 strict 模式 → ALLOW
        """
        # 1) allowlist 优先
        for argv in segments:
            prefix = extract_prefix(argv)
            if not prefix:
                continue
            rule = self.store.is_allowed(prefix, cwd)
            if rule:
                return GateResult(Decision.ALLOW, matched_rule=rule)

        # 2) warnings 命中 → 必弹
        if warnings:
            return GateResult(Decision.ASK, reason="; ".join(warnings))

        # 3) strict：所有段必须只读
        if self.strict:
            non_readonly: List[str] = []
            for argv in segments:
                if not argv:
                    continue
                if not is_read_only(argv):
                    non_readonly.append(extract_prefix(argv))
            if non_readonly:
                pretty = ", ".join(f'"{p}"' for p in non_readonly if p)
                return GateResult(
                    Decision.ASK,
                    reason=f"非只读命令（{pretty}）需用户确认",
                )

        # 4) 全只读 / 非 strict 默认放行
        return GateResult(Decision.ALLOW)

    def prompt_user(
        self,
        command: str,
        prefix: str,
        reason: str,
        cwd: str,
    ) -> GateResult:
        """REPL 同步弹窗，阻塞 stdin 读用户选择。

        非 TTY → 直接返回 DENY + permission_unavailable=True，让模型知道
        是没终端而不是命令本身被拒。
        """
        if not (sys.stdin and sys.stdin.isatty()):
            return GateResult(
                Decision.DENY,
                reason="无可用终端确认权限",
                permission_unavailable=True,
            )

        # 弹窗框
        bar = "─" * 12
        print(f"\n{bar} 需要确认执行 {bar}", flush=True)
        print(f"命令：{command}", flush=True)
        print(f"原因：{reason}", flush=True)
        print(f"工作目录：{cwd}", flush=True)
        print("", flush=True)
        print(f'  [1] 允许这一次', flush=True)
        print(f'  [2] 总是允许 "{prefix}" 在此目录', flush=True)
        print(f'  [3] 总是允许 "{prefix}" 在所有目录', flush=True)
        print(f"  [4] 拒绝", flush=True)

        try:
            answer = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return GateResult(Decision.DENY, reason="用户取消")

        if answer == "1":
            return GateResult(Decision.ALLOW, reason="本次允许")
        if answer == "2":
            rule = self.store.add_rule(prefix, "cwd", cwd)
            return GateResult(Decision.ALLOW, reason="已加入项目级 allowlist", matched_rule=rule)
        if answer == "3":
            rule = self.store.add_rule(prefix, "global")
            return GateResult(Decision.ALLOW, reason="已加入全局 allowlist", matched_rule=rule)
        # 包括 "4" 和任何不合法输入：拒绝
        return GateResult(Decision.DENY, reason="用户拒绝")


# ========== 全局单例 ==========

_gate_lock = threading.Lock()
_gate_instance: Optional[PermissionGate] = None


def get_permission_gate() -> PermissionGate:
    global _gate_instance
    if _gate_instance is None:
        with _gate_lock:
            if _gate_instance is None:
                _gate_instance = PermissionGate()
    return _gate_instance


def reset_permission_gate(
    store: Optional[PermissionStore] = None,
    strict: bool = True,
) -> PermissionGate:
    """仅测试用。"""
    global _gate_instance
    with _gate_lock:
        _gate_instance = PermissionGate(store=store, strict=strict)
    return _gate_instance
