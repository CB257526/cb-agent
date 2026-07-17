"""持久化 shell session（伪持久化）

不引入 pexpect / winpty。BashSession 只维护一个 _cwd，
每次执行命令时把 `cd <cwd> && <cmd> && <cwd marker>` 拼到命令前后；
执行完用正则从 stdout 末尾抓 marker，得到命令结束时的真实 cwd
并写回 _cwd。命令失败（marker 没出现）→ _cwd 不变。

子 agent 隔离：is_subagent=True 时，_cwd 取自父 session 但执行
后**不写回**（即使命中 marker 也丢弃），对应 Claude Code 的
preventCwdChanges。

设计取舍：
- 不持久化 env / aliases / shell function（要 pexpect，价值低）
- cwd 不写盘（仅在当前后端进程生命周期内保留）
- compose 的命令对 PowerShell / cmd / bash 各发一份等价模板
"""

from __future__ import annotations

import os
import re
import threading
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Optional

from tools.tools.bash_shell import get_shell_kind


# 用极小概率会出现在用户输出里的 marker；前后用下划线包住更稳
CWD_MARKER_PREFIX = "__CBAGENT_CWD__"
CWD_MARKER_SUFFIX = "__CBAGENT_CWD_END__"

# 抓 marker 的正则：尽量贪婪到 SUFFIX，避免 cwd 路径里有奇怪字符
_CWD_MARKER_RE = re.compile(
    re.escape(CWD_MARKER_PREFIX) + r"(.*?)" + re.escape(CWD_MARKER_SUFFIX),
    re.DOTALL,
)


def strip_cwd_marker(text: str) -> str:
    """从任意字符串里剥掉所有 cwd marker 段。

    BashTaskTool 用：后台命令的输出里 marker 没法被主 session 消费，要主动剥。
    """
    if not text:
        return text
    return _CWD_MARKER_RE.sub("", text)


# 显式切换工作目录的命令名（POSIX + cmd + PowerShell 全平台覆盖）
_CWD_CHANGE_VERBS = frozenset({
    "cd", "chdir", "pushd", "popd",
    "set-location", "sl",  # PowerShell（小写比对）
})

# 词法扫描：在不在引号里的位置匹配 cwd 关键字
# 用一个折中策略：先按常见 shell 操作符切，再看每段开头是不是 cd 类
_SEGMENT_SPLIT_RE = re.compile(r"(?:&&|\|\||;|\||\n|`)")


def command_intends_cwd_change(command: str) -> bool:
    """判断用户原始 command 是否显式包含 cd / pushd / popd / Set-Location 等。

    用途：避免命令链里"未声明的 cd 副作用"污染主 session._cwd。
    例：用户写 `cd a; dir; cd ..`，最后一段 cd 改了 cwd 是用户预期；
        用户写 `cd nonexistent; ls`，PowerShell 用 ; 继续执行，最终 marker
        落在某个意料之外的目录，绝不能写回。

    实现取舍：不依赖 bashlex（PS 命令解析失败率高），用简单的操作符切段 +
    段首关键字判断。带引号的字面量虽然可能切错，但只要段首不是 cd 关键字
    就不会误判为"想改 cwd"，最坏只是漏判（保守拒绝写回）。
    """
    if not command:
        return False
    for raw in _SEGMENT_SPLIT_RE.split(command):
        seg = raw.strip()
        if not seg:
            continue
        # 剥环境变量赋值前缀：`PATH=x cd foo`
        while seg and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg):
            sp = seg.split(None, 1)
            seg = sp[1] if len(sp) > 1 else ""
        # 剥 sudo / time 等装饰前缀（保守，只剥一层）
        first = seg.split(None, 1)[0] if seg else ""
        if first.lower() in _CWD_CHANGE_VERBS:
            return True
    return False


class BashSession:
    """单条 bash 会话。进程内通常只用 SessionManager 拿到的全局实例。"""

    def __init__(
        self,
        initial_cwd: Optional[str] = None,
        is_subagent: bool = False,
    ):
        self._cwd: str = str(Path(initial_cwd or os.getcwd()).resolve())
        self.is_subagent = is_subagent
        self._lock = threading.Lock()

    # ---------- 公共属性 ----------

    @property
    def cwd(self) -> str:
        return self._cwd

    # ---------- 命令拼装 ----------

    def compose(self, command: str, override_cwd: Optional[str] = None) -> str:
        """把原始命令包成 `cd <cwd> && <cmd> && <marker>` 形式。

        override_cwd: 本次调用的临时 cwd，不影响 self._cwd 状态机。
        """
        target_cwd = self._resolve_cwd(override_cwd)
        kind = get_shell_kind()

        if kind in ("bash", "git-bash", "wsl"):
            # POSIX 风格：echo marker 用 printf 防 echo -e 行为差异
            return (
                f'cd "{target_cwd}" && {command}; '
                f'__rc=$?; '
                f'printf "\\n{CWD_MARKER_PREFIX}%s{CWD_MARKER_SUFFIX}" "$(pwd)"; '
                f'exit $__rc'
            )
        elif kind == "powershell":
            # PowerShell：Set-Location 等价 cd；用 $LASTEXITCODE 转发
            return (
                f'Set-Location -LiteralPath "{target_cwd}"; '
                f'{command}; '
                f'$__rc = $LASTEXITCODE; '
                f'Write-Host "{CWD_MARKER_PREFIX}$($PWD.Path){CWD_MARKER_SUFFIX}"; '
                f'exit $__rc'
            )
        else:  # cmd.exe
            return (
                f'cd /d "{target_cwd}" && ({command}) & '
                f'set __rc=%ERRORLEVEL% & '
                f'echo {CWD_MARKER_PREFIX}%CD%{CWD_MARKER_SUFFIX} & '
                f'exit /b %__rc%'
            )

    # ---------- marker 解析 ----------

    def consume_cwd_marker(
        self,
        stdout: str,
        original_command: Optional[str] = None,
    ) -> tuple[str, Optional[str]]:
        """从 stdout 中抓 cwd marker，更新 _cwd（子 agent 模式不写回），
        返回 (清洗后的 stdout, 抓到的新 cwd 或 None)。

        约定：marker 必须出现在末尾，否则视为命令未走完不更新。

        original_command: 用户原始命令文本。仅当其中显式出现 cd / pushd / popd /
        Set-Location 等切目录命令时才允许把 marker 里的 cwd 写回 self._cwd。
        默认 None（不传）= 不写回，仅清理 marker。这样能防"意外副作用 cwd 漂移"。
        """
        m = _CWD_MARKER_RE.search(stdout)
        if not m:
            return stdout, None

        new_cwd = m.group(1).strip()
        cleaned = (stdout[: m.start()] + stdout[m.end():]).rstrip("\r\n")
        cleaned = cleaned.rstrip("\r\n")  # 再剥一层换行避免双空行

        allow_writeback = (
            not self.is_subagent
            and bool(new_cwd)
            and original_command is not None
            and command_intends_cwd_change(original_command)
        )
        if allow_writeback:
            with self._lock:
                self._cwd = new_cwd

        return cleaned, new_cwd if allow_writeback else None

    # ---------- 内部 ----------

    def _resolve_cwd(self, override_cwd: Optional[str]) -> str:
        """合成本次执行的目标 cwd。override_cwd 可以是相对路径，
        相对当前 self._cwd。"""
        if not override_cwd:
            return self._cwd
        p = Path(override_cwd)
        if not p.is_absolute():
            p = Path(self._cwd) / p
        return str(p.resolve())


# ========== 全局单例 ==========

_session_lock = threading.Lock()
_session_instance: Optional[BashSession] = None
_session_override: ContextVar[Optional[BashSession]] = ContextVar(
    "cbagent_bash_session_override",
    default=None,
)


def get_session() -> BashSession:
    """获取当前上下文的 BashSession；未绑定覆盖值时返回进程级会话。"""

    override = _session_override.get()
    if override is not None:
        return override
    global _session_instance
    if _session_instance is None:
        with _session_lock:
            if _session_instance is None:
                _session_instance = BashSession()
    return _session_instance


def set_session_override(session: BashSession) -> Token[Optional[BashSession]]:
    """为当前 Agent 上下文绑定独立 BashSession，并由 ToolExecutor 传播到工具线程。"""

    return _session_override.set(session)


def reset_session_override(token: Token[Optional[BashSession]]) -> None:
    """恢复进入子代理运行前的 BashSession 上下文。"""

    _session_override.reset(token)


def reset_session(initial_cwd: Optional[str] = None) -> BashSession:
    """重置全局 session（仅测试用）。"""
    global _session_instance
    with _session_lock:
        _session_instance = BashSession(initial_cwd=initial_cwd)
    return _session_instance
