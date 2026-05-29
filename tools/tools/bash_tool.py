"""Shell 命令执行工具

为 Agent 提供直接与操作系统交互的能力：执行命令、读取输出、处理超时和异常。
参考 Claude Code BashTool 的设计思路。

模块拆分：
- bash_security.py   危险命令检测（致命拦截 + 警告）
- bash_semantics.py  退出码语义解释
- bash_utils.py      命令分类（search/read/list/silent）
- bash_prompt.py     注入给模型的系统提示词
"""

import json
import logging
import os
import signal
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from tools.tools.bash_security import check_fatal, check_warnings
from tools.tools.bash_semantics import lookup_semantic
from tools.tools.bash_utils import classify_command, get_shell, wrap_command

logger = logging.getLogger(__name__)


class BashTool(Tool):
    """Shell 命令执行工具。

    允许 Agent 执行操作系统命令，读取文件内容、搜索代码、管理进程等。
    内置危险命令检测（致命拦截 + 警告）和退出码语义解释。
    """

    def __init__(self):
        super().__init__(
            name="bash",
            description=(
                "执行 Shell 命令并返回输出。"
                "用于：文件操作、代码搜索（grep/find）、程序执行、系统管理、Git 操作等。"
                "支持超时控制和后台执行。"
            ),
        )
        self._last_command = ""
        self._last_elapsed = 0.0
        self._background_tasks: Dict[str, subprocess.Popen] = {}

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
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        """执行 Shell 命令。

        流程：验证参数 → 安全检测 → 选 shell → 执行 → 输出限制 → 语义解释 → 返回 JSON。
        """
        if not self.validate_parameters(parameters):
            return json.dumps({
                "error": "参数验证失败", "stdout": "", "stderr": "",
            }, ensure_ascii=False)

        command = parameters["command"].strip()
        timeout = parameters.get("timeout", 120000) / 1000.0
        run_in_background = bool(parameters.get("run_in_background", False))

        # 安全检测
        fatal = check_fatal(command)
        if fatal:
            logger.warning("bash: 拒绝危险命令 — %s", fatal)
            return json.dumps({
                "stdout": "",
                "stderr": fatal,
                "exit_code": -1,
                "interrupted": False,
                "timeout": False,
                "semantic": "fatal",
            }, ensure_ascii=False)

        warnings = check_warnings(command)
        self._last_command = command
        t0 = time.perf_counter()
        shell = get_shell()
        # Windows: chcp 65001 先切到 UTF-8，中文才不乱码
        all_cmd = wrap_command(command)

        # 后台
        if run_in_background:
            task_id = str(uuid.uuid4())[:8]
            try:
                proc = subprocess.Popen(
                    shell + [all_cmd],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", errors="replace",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                self._background_tasks[task_id] = proc
            except Exception as e:
                return json.dumps({
                    "stdout": "", "stderr": f"后台启动失败: {e}",
                    "exit_code": -1, "interrupted": False, "timeout": False,
                    "background": False,
                }, ensure_ascii=False)
            return json.dumps({
                "stdout": "(后台运行中)",
                "stderr": "",
                "exit_code": 0,
                "interrupted": False,
                "timeout": False,
                "background": True,
                "background_task_id": task_id,
            }, ensure_ascii=False)

        # 前台同步
        proc = None
        interrupted = False
        timed_out = False

        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            preexec_fn = None if os.name == "nt" else (
                lambda: signal.signal(signal.SIGPIPE, signal.SIG_DFL))
            proc = subprocess.Popen(
                shell + [command],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
                creationflags=creationflags, preexec_fn=preexec_fn,
            )
            stdout, stderr = proc.communicate(timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            if proc:
                try:
                    if os.name == "nt":
                        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=2)
                except Exception:
                    stdout, stderr = "", "[进程已终止，输出丢失]"
                finally:
                    try: proc.kill()
                    except Exception: pass
            else:
                stdout, stderr = "", "[超时]"
            exit_code = -1
        except KeyboardInterrupt:
            interrupted = True
            stdout, stderr = "", "[用户中断]"
            exit_code = -1
        except Exception as e:
            stdout, stderr = "", str(e)
            exit_code = -1

        self._last_elapsed = time.perf_counter() - t0

        # 输出限制
        MAX_STDOUT = 100_000
        MAX_STDERR = 20_000
        if stdout and len(stdout) > MAX_STDOUT:
            stdout = stdout[:MAX_STDOUT] + f"\n\n... [{len(stdout) - MAX_STDOUT} 字符已截断] ..."
        if stderr and len(stderr) > MAX_STDERR:
            stderr = stderr[:MAX_STDERR] + f"\n... [{len(stderr) - MAX_STDERR} 字符已截断] ..."

        # 退出码语义
        semantic = lookup_semantic(command, exit_code)
        is_error = exit_code != 0
        if semantic and semantic["status"] == "ok":
            is_error = False

        # 警告前缀
        if warnings and stdout:
            stdout = "\n".join(warnings) + "\n" + stdout
        elif warnings:
            stdout = "\n".join(warnings)

        return json.dumps({
            "stdout": stdout or "",
            "stderr": stderr or "",
            "exit_code": exit_code,
            "interrupted": interrupted,
            "timeout": timed_out,
            "is_error": is_error,
            "semantic": semantic,
            "background": False,
            "classification": classify_command(command),
        }, ensure_ascii=False)


# 模块级单例
_bash_tool_instance: Optional[BashTool] = None


def get_bash_tool() -> BashTool:
    global _bash_tool_instance
    if _bash_tool_instance is None:
        _bash_tool_instance = BashTool()
    return _bash_tool_instance
