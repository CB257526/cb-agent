"""后台任务管理工具

给模型用的接口：
- list: 列出所有后台任务（含已完成）
- output: 拉某个任务的当前输出（截到末尾，最多 100KB；用 FileReadTool 拿全量）
- wait: 阻塞等任务结束或超时
- kill: 杀任务

不直接对外暴露 BackgroundRegistry 的进程对象。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from tools.tools.bash_background import BackgroundRegistry, get_background_registry
from tools.tools.bash_session import strip_cwd_marker


MAX_OUTPUT_TAIL = 100 * 1024  # 100KB


class BashTaskTool(Tool):
    def __init__(self, registry: Optional[BackgroundRegistry] = None):
        super().__init__(
            name="bash_task",
            description=(
                "管理由 bash(run_in_background=true) 启动的后台任务。"
                "支持四种 action：list 列表、output 拉输出、wait 等结束、kill 终止。"
            ),
        )
        self._registry = registry or get_background_registry()

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="list / output / wait / kill 之一。",
                required=True,
            ),
            ToolParameter(
                name="task_id",
                type="string",
                description="后台任务 id（除 list 外必填）。",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description="wait 模式的超时（秒），默认 30。",
                required=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        action = parameters.get("action")
        if action not in ("list", "output", "wait", "kill"):
            return False
        if action != "list" and not parameters.get("task_id"):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps({"error": "参数验证失败"}, ensure_ascii=False)

        action = parameters["action"]
        if action == "list":
            return self._list()

        task_id = parameters["task_id"]
        if action == "output":
            return self._output(task_id)
        if action == "wait":
            timeout = float(parameters.get("timeout", 30))
            return self._wait(task_id, timeout)
        if action == "kill":
            return self._kill(task_id)
        return json.dumps({"error": f"未知 action: {action}"}, ensure_ascii=False)

    # ---------- impl ----------

    def _list(self) -> str:
        tasks = self._registry.list()
        return json.dumps(
            {"tasks": [t.to_dict() for t in tasks]},
            ensure_ascii=False,
        )

    def _output(self, task_id: str) -> str:
        t = self._registry.get(task_id)
        if not t:
            return json.dumps({"error": f"任务不存在: {task_id}"}, ensure_ascii=False)

        log = Path(t.output_path)
        content = ""
        truncated = False
        if log.exists():
            try:
                size = log.stat().st_size
                with open(log, "rb") as f:
                    if size > MAX_OUTPUT_TAIL:
                        f.seek(size - MAX_OUTPUT_TAIL)
                        truncated = True
                    raw = f.read()
                content = raw.decode("utf-8", errors="replace")
            except OSError as e:
                content = f"[读取日志失败: {e}]"

        # 剥掉后台命令注入的 cwd marker（后台不回写主 session cwd）
        content = strip_cwd_marker(content)

        return json.dumps(
            {
                "task": t.to_dict(),
                "output": content,
                "output_truncated": truncated,
                "hint_file_read": (
                    f"完整输出位于 {t.output_path}，可用 file_read 拉指定行号"
                    if truncated else None
                ),
            },
            ensure_ascii=False,
        )

    def _wait(self, task_id: str, timeout: float) -> str:
        t = self._registry.wait(task_id, timeout=timeout)
        if not t:
            return json.dumps({"error": f"任务不存在: {task_id}"}, ensure_ascii=False)
        return json.dumps({"task": t.to_dict()}, ensure_ascii=False)

    def _kill(self, task_id: str) -> str:
        t = self._registry.kill(task_id)
        if not t:
            return json.dumps({"error": f"任务不存在: {task_id}"}, ensure_ascii=False)
        return json.dumps({"task": t.to_dict()}, ensure_ascii=False)
