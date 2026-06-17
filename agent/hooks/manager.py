"""HookManager —— hooks 控制流核心。

与 EventBus 的根本区别（也是为什么不复用 EventBus）：

- EventBus 单向广播、不收返回值、吞异常；HookManager 双向，要收集 hook 的
  决策（阻止/改写/注入）并回传给调用方。
- HookManager 触发时**反过来**通过 event_bus emit HookStarted / HookCompleted，
  让前端可见。即「控制流走 HookManager，可见性走 EventBus」。

执行模型：
- ``fire(event_name, payload, matcher_value)`` 找出所有命中的 command handler，
  按配置顺序**同步串行**执行，把各自决策合并成一个 HookOutcome。
- 单个 handler 抛异常/超时按「非阻塞错误」处理：记 warning，不影响其它 handler，
  不阻止主流程。只有 handler 明确返回 deny / exit code 2 / continue=false 才阻断。

并发安全：HookManager 加载后无可变状态，fire() 可重入；command 走
subprocess.run（阻塞但线程安全），可在 ToolExecutor 的并发 worker 线程里调用。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import HookHandler, HooksConfig
from .matcher import matches

logger = logging.getLogger(__name__)


# Windows 上 PATH 里的 ``bash`` 往往先命中 System32\\bash.exe（WSL 启动器），
# 而非 Git Bash。WSL 没装发行版时它会失败并输出 UTF-16 提示，污染 hook 结果。
# 因此 Windows 下显式探测 Git Bash 路径，找不到再退回裸 "bash"（用户自担）。
def _find_git_bash() -> str:
    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
    ]
    # 从 git 可执行位置推断（…\Git\cmd\git.exe → …\Git\bin\bash.exe）
    git_exe = shutil.which("git")
    if git_exe:
        git_root = Path(git_exe).resolve().parent.parent
        candidates.insert(0, str(git_root / "bin" / "bash.exe"))
    for c in candidates:
        if os.path.isfile(c):
            return c
    logger.warning("未找到 Git Bash，回退到 PATH 中的 bash（可能命中 WSL）")
    return "bash"


# 进程内缓存一次探测结果。
_WIN_BASH = _find_git_bash() if sys.platform == "win32" else None


@dataclass
class HookOutcome:
    """一组 hook 执行后的合并决策。调用方据此改变行为。

    - blocked: 阻止主操作（PreToolUse deny / UserPromptSubmit 拦截）
    - block_reason: 阻止原因，回灌给模型
    - updated_input: PreToolUse 改写后的工具输入（None 表示不改写）
    - additional_context: 注入给模型的额外上下文（多个 hook 的输出按顺序拼接）
    - stop: continue=false，调用方应让整个 chat 收尾
    """

    blocked: bool = False
    block_reason: str = ""
    updated_input: Optional[Dict[str, Any]] = None
    additional_context: str = ""
    stop: bool = False


@dataclass
class _RawHookResult:
    """单条 command 执行的原始结果。"""

    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class HookManager:
    """加载后只读、可重入的 hook 调度器。"""

    def __init__(
        self,
        config: HooksConfig,
        *,
        event_bus: Any = None,
        cwd: Path,
        session_id: str = "",
    ) -> None:
        """
        Args:
            config: load_hooks_config 的返回值（事件名 -> [HookGroup]）
            event_bus: 可选 EventBus，用于 emit HookStarted / HookCompleted
            cwd: hook 命令的工作目录（也写进 stdin JSON）
            session_id: 当前会话 id，写进 stdin JSON
        """
        self._config = config or {}
        self._bus = event_bus
        self._cwd = Path(cwd)
        self._session_id = session_id

    @property
    def enabled(self) -> bool:
        """是否配置了任何 hook。无配置时调用方可整体跳过。"""
        return bool(self._config)

    def has_event(self, event_name: str) -> bool:
        """某事件是否配置了 hook。调用方用它避免无谓的 payload 构造。"""
        return bool(self._config.get(event_name))

    def fire(
        self,
        event_name: str,
        payload: Dict[str, Any],
        *,
        matcher_value: str = "",
        round_idx: int = 0,
    ) -> HookOutcome:
        """触发某事件的所有命中 hook，合并成 HookOutcome。

        Args:
            event_name: 事件名（如 "PreToolUse"）
            payload: 事件数据，拼进 stdin JSON（如 {"tool_name", "tool_input"}）
            matcher_value: 用于 matcher 匹配的字段（工具事件传 tool_name）
            round_idx: 当前轮次，仅用于事件展示
        """
        groups = self._config.get(event_name)
        if not groups:
            return HookOutcome()

        # 收集所有命中的 handler（保配置顺序）
        handlers: List[HookHandler] = []
        for group in groups:
            if matches(group.matcher, matcher_value):
                handlers.extend(group.handlers)
        if not handlers:
            return HookOutcome()

        stdin_json = self._build_stdin_json(event_name, payload)
        outcome = HookOutcome()
        context_parts: List[str] = []

        for handler in handlers:
            self._emit_started(event_name, handler, matcher_value, round_idx)
            start = time.perf_counter()
            raw = self._run_command(handler, stdin_json)
            duration = time.perf_counter() - start
            self._merge(raw, outcome, context_parts)
            self._emit_completed(event_name, outcome, bool(context_parts), duration, round_idx)
            # 已被阻止/要求停止则不再跑后续 handler（语义上已经短路）
            if outcome.blocked or outcome.stop:
                break

        outcome.additional_context = "\n".join(p for p in context_parts if p)
        return outcome

    # ---------- 单条 command 执行 ----------

    def _run_command(self, handler: HookHandler, stdin_json: str) -> _RawHookResult:
        """subprocess 执行单条 command，传 stdin JSON，收 stdout/stderr/exit code。

        异常/超时统一收敛成 _RawHookResult，绝不向上抛——hook 失败不该崩主流程。
        """
        shell_exe = self._resolve_shell(handler.shell)
        try:
            proc = subprocess.run(
                [shell_exe, "-c", handler.command] if shell_exe != "powershell"
                else ["powershell", "-NoProfile", "-Command", handler.command],
                input=stdin_json,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(self._cwd),
                timeout=handler.timeout,
            )
            return _RawHookResult(
                exit_code=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "hook 命令超时，按非阻塞错误处理: command=%r timeout=%ss",
                handler.command, handler.timeout,
            )
            return _RawHookResult(exit_code=-1, stdout="", stderr="hook timed out", timed_out=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("hook 命令执行异常，按非阻塞错误处理: command=%r error=%s", handler.command, e)
            return _RawHookResult(exit_code=-1, stdout="", stderr=str(e))

    def _resolve_shell(self, shell: Optional[str]) -> str:
        """解析 handler.shell：None 跟随系统（Windows→Git Bash / POSIX→sh）。

        Windows 宿主按 CLAUDE.md 约定走 Git Bash（显式路径，避开 WSL 的
        System32\\bash.exe）；显式 "powershell" 走 PowerShell（在 _run_command
        里特判命令行格式）；显式 "bash" 在 Windows 上也解析到 Git Bash。
        """
        if shell == "powershell":
            return "powershell"
        if sys.platform == "win32":
            # Windows：bash / 跟随系统都解析到 Git Bash 真实路径
            return _WIN_BASH or "bash"
        if shell == "bash":
            return "bash"
        # POSIX 跟随系统
        return "sh"

    # ---------- 决策合并 ----------

    def _merge(self, raw: _RawHookResult, outcome: HookOutcome, context_parts: List[str]) -> None:
        """把单条 command 的原始结果合并进累积 outcome。

        优先级：先解析 stdout JSON 决策（结构化、信息最全），再按 exit code 兜底。
        """
        decision = self._parse_stdout_json(raw.stdout)

        if decision is not None:
            # 顶层 continue=false → stop
            if decision.get("continue") is False:
                outcome.stop = True
                reason = decision.get("stopReason") or decision.get("reason") or ""
                if reason and not outcome.block_reason:
                    outcome.block_reason = str(reason)
            # 顶层 decision=block → blocked
            if decision.get("decision") == "block":
                outcome.blocked = True
                outcome.block_reason = str(decision.get("reason") or outcome.block_reason or "hook 阻止了该操作")
            # hookSpecificOutput
            hso = decision.get("hookSpecificOutput")
            if isinstance(hso, dict):
                if hso.get("permissionDecision") == "deny":
                    outcome.blocked = True
                    outcome.block_reason = str(
                        hso.get("permissionDecisionReason")
                        or outcome.block_reason
                        or "hook 拒绝了该工具调用"
                    )
                updated = hso.get("updatedInput")
                if isinstance(updated, dict):
                    outcome.updated_input = updated
                ctx = hso.get("additionalContext")
                if isinstance(ctx, str) and ctx.strip():
                    context_parts.append(ctx.strip())
            return

        # 无 JSON 决策：按 exit code 兜底
        if raw.exit_code == 2:
            # 阻塞错误：stderr 反馈给模型并阻止操作
            outcome.blocked = True
            outcome.block_reason = (raw.stderr or "hook 以 exit code 2 阻止了该操作").strip()
        elif raw.exit_code not in (0,):
            # 其它非零：非阻塞错误，已在 _run_command 记 warning，这里不改 outcome
            logger.warning(
                "hook 返回非阻塞错误码: exit_code=%s stderr=%s",
                raw.exit_code, (raw.stderr or "")[:200],
            )

    @staticmethod
    def _parse_stdout_json(stdout: str) -> Optional[Dict[str, Any]]:
        """尝试把 stdout 解析成决策 JSON。非 JSON 返回 None（按 exit code 兜底）。"""
        text = (stdout or "").strip()
        if not text or not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    # ---------- stdin 构造 ----------

    def _build_stdin_json(self, event_name: str, payload: Dict[str, Any]) -> str:
        """拼装传给 hook 命令 stdin 的 JSON（对齐 Claude Code 字段名）。"""
        data: Dict[str, Any] = {
            "session_id": self._session_id,
            "cwd": str(self._cwd),
            "hook_event_name": event_name,
        }
        data.update(payload or {})
        return json.dumps(data, ensure_ascii=False)

    # ---------- 事件 emit（可见性走 EventBus）----------

    def _emit_started(
        self, event_name: str, handler: HookHandler, matcher: str, round_idx: int,
    ) -> None:
        if self._bus is None:
            return
        try:
            from agent.events import HookStarted
            self._bus.emit(HookStarted(
                event_name=event_name,
                handler_type=handler.type,
                matcher=matcher,
                round_idx=round_idx,
            ))
        except Exception:  # noqa: BLE001
            logger.exception("emit HookStarted 失败，已吞")

    def _emit_completed(
        self, event_name: str, outcome: HookOutcome, has_context: bool,
        duration: float, round_idx: int,
    ) -> None:
        if self._bus is None:
            return
        try:
            from agent.events import HookCompleted
            self._bus.emit(HookCompleted(
                event_name=event_name,
                blocked=outcome.blocked,
                has_context=has_context,
                duration_seconds=duration,
                round_idx=round_idx,
            ))
        except Exception:  # noqa: BLE001
            logger.exception("emit HookCompleted 失败，已吞")


__all__ = ["HookManager", "HookOutcome"]
