"""Shell 命令执行工具

为 Agent 提供直接与操作系统交互的能力：执行命令、读取输出、处理超时和异常。
参考 Claude Code BashTool 设计：

模块拆分：
- bash_security.py   危险命令检测（致命拦截 + 警告）
- bash_semantics.py  退出码语义解释
- bash_classify.py   命令分类（search/read/list/silent）
- bash_shell.py      shell 检测、命令包装、平台提示
- bash_session.py    持久化 cwd 的会话状态机
- bash_prompt.py     注入给模型的系统提示词
"""

import json
import logging
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from agent.cancel import get_current_cancel_token
from tools.tool import Tool, ToolParameter
from tools.tools.bash_security import check_fatal, check_warnings, parse_pipeline
from tools.tools.bash_semantics import lookup_semantic
from tools.tools.bash_classify import classify_command
from tools.tools.bash_shell import get_shell, wrap_command
from tools.tools.bash_session import BashSession, get_session
from tools.tools.bash_output import process_output, default_output_dir
from tools.tools.bash_background import get_background_registry
from tools.tools.bash_permission import (
    Decision, PermissionGate, extract_prefix, get_permission_gate,
)

logger = logging.getLogger(__name__)


# UI 预览相关常量。仅影响 __display__ 字段，不影响给 LLM 的 stdout/stderr。
_DISPLAY_STDOUT_PREVIEW = 800
_DISPLAY_STDERR_PREVIEW = 400


def _foreground_process_group_options() -> Dict[str, Any]:
    """返回前台命令的独立进程组选项。

    超时处理需要终止命令及其派生进程，因此子进程必须与 cb-agent 自身进程组隔离。
    POSIX 使用 start_new_session，Windows 使用 CREATE_NEW_PROCESS_GROUP。
    """
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"creationflags": 0, "start_new_session": True}


def _signal_process_tree(proc: subprocess.Popen, *, force: bool) -> bool:
    """向独立的命令进程组发送终止信号。

    返回 True 表示已完成进程组级处理；返回 False 时调用方只能安全地终止直接
    子进程。POSIX 下必须先确认目标进程组不是 cb-agent 自身进程组，避免再次出现
    Bash 超时把 Python 后端和 OTUI 一并杀死的问题。
    """
    if os.name == "nt":
        try:
            if force:
                completed = subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                    timeout=5,
                )
                return completed.returncode == 0
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    try:
        target_pgid = os.getpgid(proc.pid)
        own_pgid = os.getpgrp()
    except ProcessLookupError:
        # 独立会话的组长可能已经退出，但后代仍持有 stdout/stderr 管道。创建会话时
        # 已保证 pid 同时也是 pgid，因此继续尝试终止该组，避免留下孤儿进程。
        target_pgid = proc.pid
        own_pgid = os.getpgrp()
    except OSError as exc:
        logger.warning("读取 Bash 子进程组失败: pid=%s error=%s", proc.pid, exc)
        return False

    if target_pgid <= 0 or target_pgid == own_pgid:
        logger.error(
            "拒绝向未隔离的 Bash 进程组发送信号: pid=%s target_pgid=%s own_pgid=%s",
            proc.pid,
            target_pgid,
            own_pgid,
        )
        return False

    try:
        os.killpg(target_pgid, signal.SIGKILL if force else signal.SIGTERM)
        return True
    except ProcessLookupError:
        return True
    except OSError as exc:
        logger.warning(
            "终止 Bash 进程组失败: pid=%s pgid=%s force=%s error=%s",
            proc.pid,
            target_pgid,
            force,
            exc,
        )
        return False


def _stop_process_tree(proc: subprocess.Popen) -> tuple[str, str]:
    """先温和、后强制地结束命令进程树，并尽量回收已经产生的输出。"""
    if not _signal_process_tree(proc, force=False):
        try:
            proc.terminate()
        except OSError:
            pass

    try:
        return proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    except Exception as exc:
        logger.warning("等待 Bash 子进程退出失败，将强制清理: pid=%s error=%s", proc.pid, exc)

    if not _signal_process_tree(proc, force=True):
        try:
            proc.kill()
        except OSError:
            pass

    try:
        return proc.communicate(timeout=2)
    except Exception as exc:
        logger.warning("回收 Bash 子进程输出失败: pid=%s error=%s", proc.pid, exc)
        try:
            proc.kill()
        except OSError:
            pass
        return "", "[进程已终止，输出丢失]"


def _clip(s: str, n: int) -> str:
    """字符级截断，超长追加提示。"""
    if len(s) <= n:
        return s
    return s[:n] + f"\n... [+{len(s) - n} chars]"


def _build_bash_display(
    *,
    stdout: str = "",
    stderr: str = "",
    exit_code: int = 0,
    is_error: bool = False,
    interrupted: bool = False,
    timeout: bool = False,
    background: bool = False,
    background_task_id: Optional[str] = None,
    error_override: Optional[str] = None,
) -> str:
    """生成 bash 工具的 UI 预览文本（无 ANSI 颜色，前端自行着色）。

    规则：
    - error_override（参数验证失败 / fatal 拒绝 / 权限拒绝）：直接输出 "✗ <reason>"
    - background：单行 "⟳ 后台运行中 (task <id>)"
    - timeout / interrupted：单行标记
    - is_error：首行 "✗ exit N"，下面 stderr → stdout（各 ≤ 800 字）
    - 正常：stdout（≤ 800 字），如有 stderr 追加 ≤ 400 字
    - 全空：返回 "Done."
    """
    if error_override:
        return f"✗ {error_override}"

    if background:
        return f"⟳ 后台运行中 (task {background_task_id or '?'})"

    if timeout:
        return "⏱ 命令超时"

    if interrupted:
        return "✗ 命令被中断"

    stdout = (stdout or "").rstrip()
    stderr = (stderr or "").rstrip()
    parts: list[str] = []

    if is_error:
        parts.append(f"✗ exit {exit_code}")
        if stderr:
            parts.append(_clip(stderr, _DISPLAY_STDOUT_PREVIEW))
        if stdout:
            parts.append(_clip(stdout, _DISPLAY_STDOUT_PREVIEW))
    else:
        if stdout:
            parts.append(_clip(stdout, _DISPLAY_STDOUT_PREVIEW))
        if stderr:
            parts.append(_clip(stderr, _DISPLAY_STDERR_PREVIEW))

    if not parts:
        return "Done."

    return "\n".join(parts)


def _gate_result_to_dict(gate_res) -> Optional[Dict[str, Any]]:
    """把 GateResult 拍平成 JSON 可读字段，给模型用。

    None / 子 agent 路径 → None
    其他 → {decision, reason, matched_rule, permission_unavailable}

    matched_rule 非空 → 模型可断言"已加入 allowlist"或"命中既有 allowlist"
    matched_rule 为空 + reason="本次允许" → "用户选了允许这一次"，下次会再问
    """
    if gate_res is None:
        return None
    return {
        "decision": gate_res.decision.value,
        "reason": gate_res.reason,
        "matched_rule": (
            gate_res.matched_rule.to_dict() if gate_res.matched_rule else None
        ),
        "permission_unavailable": gate_res.permission_unavailable,
        "user_feedback": gate_res.user_feedback,
    }


def _dangerously_skipped_permission_dict() -> Dict[str, Any]:
    """危险跳过模式的审计字段。

    这里不伪装成 allowlist 命中，而是显式标记 dangerously_skipped，方便日志、
    TUI 工具卡片和模型都能识别：这次放行来自启动参数，而不是用户逐条确认。
    """

    return {
        "decision": "allow",
        "reason": "启动参数 --dangerously-skip-permissions 已跳过 Bash 权限系统",
        "matched_rule": None,
        "permission_unavailable": False,
        "dangerously_skipped": True,
    }


class BashTool(Tool):
    """Shell 命令执行工具。

    允许 Agent 执行操作系统命令，读取文件内容、搜索代码、管理进程等。
    内置危险命令检测（致命拦截 + 警告）、退出码语义解释、cwd 持久化。
    """

    def __init__(
        self,
        session: Optional[BashSession] = None,
        permission: Optional[PermissionGate] = None,
        is_subagent: bool = False,
        question_channel: Optional[Any] = None,
        skill_observer: Optional[Any] = None,
        dangerously_skip_permissions: bool = False,
        dangerously_skip_permissions_provider: Optional[Callable[[], bool]] = None,
        output_dir: Optional[Path] = None,
    ):
        permission_note = (
            "当前进程已开启 --dangerously-skip-permissions，Bash 权限确认和高危命令拦截都会跳过。"
            if dangerously_skip_permissions
            else "高危命令（force push、TRUNCATE 等）会触发用户确认弹窗。"
        )
        super().__init__(
            name="bash",
            description=(
                "执行 Shell 命令并返回输出。"
                "用于：文件操作、程序执行、系统管理、Git 操作等。"
                "代码搜索和目录浏览请优先使用 grep/glob/ls 专用工具，只有专用工具无法满足时再用 bash。"
                "支持超时控制和后台执行。"
                "工作目录在多次调用之间持久化，cd 命令会被记住。"
                f"{permission_note}"
            ),
        )
        # 子 agent 用独立 session 视图，避免污染主 agent 的 cwd
        if session is not None:
            self._session = session
        elif is_subagent:
            parent = get_session()
            self._session = BashSession(initial_cwd=parent.cwd, is_subagent=True)
        else:
            self._session = get_session()

        self._permission = permission or get_permission_gate()
        # 注入了 question_channel 就把它绑到 gate 上，让 prompt_user 走 UI 弹框
        if question_channel is not None and self._permission.question_channel is None:
            self._permission.question_channel = question_channel
        self._is_subagent = is_subagent
        self._skill_observer = skill_observer
        # 危险模式由显式启动参数开启。开启后 BashTool 不做 fatal 拦截，也不走
        # PermissionGate 弹窗，所有命令直接交给系统 shell。warnings 仍会计算并写入
        # 结果，作为最基本的审计线索。
        self._dangerously_skip_permissions = dangerously_skip_permissions
        self._dangerously_skip_permissions_provider = dangerously_skip_permissions_provider
        # 子代理会注入任务专属目录，避免大输出落盘后被其它会话直接读取。
        self._output_dir = Path(output_dir or default_output_dir()).resolve()

        self._last_command = ""
        self._last_elapsed = 0.0
        # 后台任务托管在 BackgroundRegistry（进程级单例）

    # ========== Tool ABC ==========

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        command = parameters.get("command")
        if not command or not isinstance(command, str) or not command.strip():
            return False
        timeout = parameters.get("timeout")
        if timeout is not None:
            if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 600000:
                return False
        cwd = parameters.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            return False
        return True

    def get_parameters(self) -> list:
        return [
            ToolParameter(
                name="command",
                type="string",
                description=(
                    "要执行的 Shell 命令。"
                    "多个独立命令可并行发起多次 Bash 调用；"
                    "依赖命令用 && 串接；仅串行不关心失败用 ;"
                ),
                required=True,
            ),
            ToolParameter(
                name="description",
                type="string",
                description=(
                    "用主动语态描述这条命令做什么，5-10 词。"
                    "如 '列出当前目录文件'、'搜索 Python 文件中的 TODO'。"
                ),
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description="超时毫秒，默认 120000（2 分钟），最大 600000（10 分钟）。",
                required=False,
            ),
            ToolParameter(
                name="run_in_background",
                type="boolean",
                description=(
                    "设为 true 后台运行，不等待结果。"
                    "用于长时间命令如 npm install、docker build。"
                ),
                required=False,
                default=False,
            ),
            ToolParameter(
                name="cwd",
                type="string",
                description=(
                    "可选，本次调用的工作目录覆盖（仅本次生效，不影响后续调用）。"
                    "可用相对路径（相对当前 session cwd）或绝对路径。"
                    "正常情况下使用 cd 切换更直观。"
                ),
                required=False,
            ),
        ]

    def dangerously_skip_permissions_enabled(self) -> bool:
        if self._dangerously_skip_permissions_provider is None:
            return bool(self._dangerously_skip_permissions)
        try:
            return bool(self._dangerously_skip_permissions_provider())
        except Exception:
            logger.exception("bash permission mode provider failed")
            return bool(self._dangerously_skip_permissions)

    def run(self, parameters: Dict[str, Any]) -> str:
        """执行 Shell 命令。

        流程：
            验证 → 安全检测 → session.compose（注入 cd + cwd marker）→
            选 shell + wrap → Popen → 解析 marker → 输出限制 → 语义解释 → JSON
        """
        if not self.validate_parameters(parameters):
            return json.dumps({
                "error": "参数验证失败", "stdout": "", "stderr": "",
                "__display__": _build_bash_display(error_override="参数验证失败"),
            }, ensure_ascii=False)

        command = parameters["command"].strip()
        timeout = parameters.get("timeout", 120000) / 1000.0
        run_in_background = bool(parameters.get("run_in_background", False))
        override_cwd = parameters.get("cwd")

        # 安全检测。危险跳过模式下连 fatal 也放行，确保启动参数语义是“完全权限”；
        # 这种模式只应在受信任环境中手动开启。
        dangerously_skip_permissions = self.dangerously_skip_permissions_enabled()
        fatal = None if dangerously_skip_permissions else check_fatal(command)
        if fatal:
            logger.warning("bash: 拒绝危险命令 — %s", fatal)
            return json.dumps({
                "stdout": "",
                "stderr": fatal,
                "exit_code": -1,
                "cwd": self._session.cwd,
                "interrupted": False,
                "timeout": False,
                "is_error": True,
                "semantic": "fatal",
                "background": False,
                "classification": classify_command(command),
                "permission": None,
                "__display__": _build_bash_display(error_override=fatal),
            }, ensure_ascii=False)

        warnings = check_warnings(command)

        # 权限决策：所有命令都过 gate.evaluate。
        # - 子 agent 无法安全弹出交互审批；只读或已由父会话 allowlist 授权的命令
        #   可以执行，原本需要 ASK 的命令保守拒绝并交回主 Agent 处理。
        # - 主 agent：fatal 上游已拦；只读命令直接 ALLOW；warnings 或非只读 → 弹窗。
        gate_res = None
        permission_payload = None
        if dangerously_skip_permissions:
            logger.warning(
                "bash: --dangerously-skip-permissions 已启用，跳过安全拦截和权限确认: %s",
                command,
            )
            permission_payload = _dangerously_skipped_permission_dict()
        elif self._is_subagent:
            segments = parse_pipeline(command)
            gate_res = self._permission.evaluate(
                command, segments, warnings, self._session.cwd,
            )
            if gate_res.decision != Decision.ALLOW:
                reason = gate_res.reason or "该命令需要父会话交互审批"
                stderr = (
                    "[子代理权限拒绝] 后台子代理不能代替用户确认命令："
                    f"{reason}。请让主 Agent 执行或先配置明确 allowlist。"
                )
                logger.warning("bash: 子代理权限拒绝 — %s", reason)
                return json.dumps({
                    "stdout": "",
                    "stderr": stderr,
                    "exit_code": -1,
                    "cwd": self._session.cwd,
                    "interrupted": False,
                    "timeout": False,
                    "is_error": True,
                    "semantic": "permission_denied",
                    "background": False,
                    "classification": classify_command(command),
                    "permission_unavailable": True,
                    "warnings": warnings,
                    "permission": _gate_result_to_dict(gate_res),
                    "session_cancelled": False,
                    "__display__": _build_bash_display(error_override=stderr),
                }, ensure_ascii=False)
            permission_payload = _gate_result_to_dict(gate_res)
        elif not self._is_subagent:
            segments = parse_pipeline(command)
            gate_res = self._permission.evaluate(
                command, segments, warnings, self._session.cwd,
            )
            if gate_res.decision == Decision.ASK:
                # 取第一段命令作为弹窗前缀
                prefix = ""
                for argv in segments:
                    p = extract_prefix(argv)
                    if p:
                        prefix = p
                        break
                gate_res = self._permission.prompt_user(
                    command, prefix, gate_res.reason, self._session.cwd,
                )
            if gate_res.decision == Decision.DENY:
                logger.warning("bash: 权限拒绝 — %s", gate_res.reason)
                if gate_res.user_feedback:
                    feedback_msg = (
                        "[权限反馈] 用户没有授权执行该 bash 命令，并给出了替代建议："
                        f"{gate_res.user_feedback}"
                    )
                    return json.dumps({
                        "stdout": "",
                        "stderr": feedback_msg,
                        "exit_code": -1,
                        "cwd": self._session.cwd,
                        "interrupted": False,
                        "timeout": False,
                        "is_error": True,
                        "semantic": None,
                        "background": False,
                        "classification": classify_command(command),
                        "permission_unavailable": gate_res.permission_unavailable,
                        "warnings": warnings,
                        "permission": _gate_result_to_dict(gate_res),
                        "permission_feedback": gate_res.user_feedback,
                        "session_cancelled": False,
                        "__display__": _build_bash_display(error_override=feedback_msg),
                    }, ensure_ascii=False)
                # 用户已经明确拒绝本次 bash 执行时，应结束当前 agent 回合，而不是
                # 让模型在下一轮继续尝试同一个受限能力。这里通过当前 CancelToken
                # 走统一取消路径，OTUI/QQ/微信都会收到 Cancelled 事件并释放 busy。
                token = get_current_cancel_token()
                if token is not None:
                    token.cancel()
                return json.dumps({
                    "stdout": "",
                    "stderr": f"[权限拒绝] {gate_res.reason}",
                    "exit_code": -1,
                    "cwd": self._session.cwd,
                    "interrupted": False,
                    "timeout": False,
                    "is_error": True,
                    "semantic": None,
                    "background": False,
                    "classification": classify_command(command),
                    "permission_unavailable": gate_res.permission_unavailable,
                    "warnings": warnings,
                    "permission": _gate_result_to_dict(gate_res),
                    "session_cancelled": True,
                    "__display__": _build_bash_display(
                        error_override=f"[权限拒绝] {gate_res.reason}",
                    ),
                }, ensure_ascii=False)
            permission_payload = _gate_result_to_dict(gate_res)

        execution_cwd = self._effective_cwd(override_cwd)
        skill_script_hits = self._record_skill_script_hits(command, execution_cwd)

        self._last_command = command
        t0 = time.perf_counter()

        # session 包装：注入 cd <cwd> 前缀 + cwd marker 后缀
        composed = self._session.compose(command, override_cwd=override_cwd)
        shell = get_shell()
        all_cmd = wrap_command(composed)

        # 后台分支：暂沿用旧字典实现，commit 6 抽出到 BackgroundRegistry
        if run_in_background:
            return self._run_background(
                command,
                shell,
                all_cmd,
                warnings,
                permission_payload,
                skill_script_hits=skill_script_hits,
            )

        # 前台同步
        proc = None
        interrupted = False
        timed_out = False
        stdout = ""
        stderr = ""
        exit_code = -1

        try:
            process_group_options = _foreground_process_group_options()
            proc = subprocess.Popen(
                shell + [all_cmd],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                **process_group_options,
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            if proc:
                logger.warning("Bash 命令超时，开始清理独立进程组: pid=%s timeout=%.2fs", proc.pid, timeout)
                stdout, stderr = _stop_process_tree(proc)
            else:
                stdout, stderr = "", "[超时]"
            exit_code = -1
        except KeyboardInterrupt:
            interrupted = True
            if proc:
                stdout, stderr = _stop_process_tree(proc)
            if not stderr:
                stderr = "[用户中断]"
            exit_code = -1
        except Exception as e:
            stdout, stderr = "", str(e)
            exit_code = -1

        self._last_elapsed = time.perf_counter() - t0

        # 解析并消费 cwd marker（更新 session._cwd，并从 stdout 剔除 marker 行）
        # 只在用户原始命令显式包含 cd/pushd/Set-Location 时才允许写回，
        # 防止"未声明的 cd 副作用"污染主 session（例：`cd nonexistent; ls` 链式失败漂移）
        stdout, _ = self._session.consume_cwd_marker(stdout or "", original_command=command)

        # 输出处理：内存截断 + 视情况落盘到 ./.cbagent/bash_outputs/<task_id>.log
        task_id = uuid.uuid4().hex[:12]
        processed = process_output(
            stdout or "",
            stderr or "",
            output_dir=self._output_dir,
            task_id=task_id,
        )

        # 退出码语义
        semantic = lookup_semantic(command, exit_code)
        is_error = exit_code != 0
        if semantic and semantic["status"] == "ok":
            is_error = False
        # 截断后无法可靠落盘或命中硬上限时，命令本身即使退出码为 0，
        # 本次工具结果也不可作为完整事实使用，必须显式标记失败。
        if processed.persist_error or processed.hard_limit_exceeded:
            is_error = True

        return json.dumps({
            "stdout": processed.stdout,
            "stderr": processed.stderr,
            "exit_code": exit_code,
            "cwd": self._session.cwd,
            "interrupted": interrupted,
            "timeout": timed_out,
            "is_error": is_error,
            "semantic": semantic,
            "background": False,
            "classification": classify_command(command),
            "warnings": warnings,
            "skill_script_hits": skill_script_hits,
            "output_truncated": processed.output_truncated,
            "output_file": processed.output_file,
            "stdout_file": processed.stdout_file,
            "stderr_file": processed.stderr_file,
            "stdout_chars": processed.stdout_chars,
            "stderr_chars": processed.stderr_chars,
            "stdout_bytes": processed.stdout_bytes,
            "stderr_bytes": processed.stderr_bytes,
            "stdout_lines": processed.stdout_lines,
            "stderr_lines": processed.stderr_lines,
            "hard_limit_exceeded": processed.hard_limit_exceeded,
            "persist_error": processed.persist_error,
            "permission": permission_payload,
            "__display__": _build_bash_display(
                stdout=processed.stdout,
                stderr=processed.stderr,
                exit_code=exit_code,
                is_error=is_error,
                interrupted=interrupted,
                timeout=timed_out,
            ),
        }, ensure_ascii=False)

    # ========== 后台执行（走 BackgroundRegistry） ==========

    def _run_background(
        self,
        command,
        shell,
        all_cmd,
        warnings,
        permission_payload=None,
        skill_script_hits=None,
    ) -> str:
        registry = get_background_registry()
        task_id = uuid.uuid4().hex[:12]
        try:
            task = registry.spawn(
                task_id=task_id,
                command=command,
                argv=shell + [all_cmd],
                cwd=self._session.cwd,
            )
        except Exception as e:
            return json.dumps({
                "stdout": "", "stderr": f"后台启动失败: {e}",
                "exit_code": -1, "cwd": self._session.cwd,
                "interrupted": False, "timeout": False, "is_error": True,
                "background": False,
                "classification": classify_command(command),
                "warnings": warnings,
                "skill_script_hits": skill_script_hits or [],
                "permission": permission_payload,
                "__display__": _build_bash_display(error_override=f"后台启动失败: {e}"),
            }, ensure_ascii=False)
        return json.dumps({
            "stdout": (
                f"(后台运行中) task_id={task.id}\n"
                f"输出文件: {task.output_path}\n"
                "可用 bash_task(action=output|wait|list|kill) 查询。"
            ),
            "stderr": "",
            "exit_code": 0,
            "cwd": self._session.cwd,
            "interrupted": False,
            "timeout": False,
            "is_error": False,
            "background": True,
            "background_task_id": task.id,
            "output_file": task.output_path,
            "classification": classify_command(command),
            "warnings": warnings,
            "skill_script_hits": skill_script_hits or [],
            "permission": permission_payload,
            "__display__": _build_bash_display(
                background=True, background_task_id=task.id,
            ),
        }, ensure_ascii=False)

    def _effective_cwd(self, override_cwd: Optional[str]) -> str:
        try:
            if not override_cwd:
                return self._session.cwd
            path = Path(override_cwd)
            if not path.is_absolute():
                path = Path(self._session.cwd) / path
            return str(path.resolve())
        except Exception:
            logger.exception("bash effective cwd resolution failed")
            return self._session.cwd

    def _record_skill_script_hits(self, command: str, cwd: str) -> list[dict[str, str]]:
        observer = self._skill_observer
        if observer is None:
            return []
        record = getattr(observer, "record_script_hits", None)
        if not callable(record):
            return []
        try:
            result = record(command, cwd=cwd)
            return list(result or [])
        except Exception:
            logger.exception("skill script hit observer failed")
            return []


# 模块级单例
_bash_tool_instance: Optional[BashTool] = None


def get_bash_tool() -> BashTool:
    global _bash_tool_instance
    if _bash_tool_instance is None:
        _bash_tool_instance = BashTool()
    return _bash_tool_instance
