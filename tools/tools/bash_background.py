"""后台任务注册中心

参考 Claude Code 的 BashTool 后台流：
- 启动时把 stdout/stderr 直接重定向到落盘文件（不用 PIPE，否则 buffer 满会阻塞子进程）
- 单进程内单例，所有 BashTool 共享一份字典
- 完成通知通过 drain_notifications() 拉，给 AgentRunner 在每轮 think 前注入 system message
- 跨平台杀进程：Windows CTRL_BREAK_EVENT → 2s 后 taskkill /T /F；POSIX SIGTERM → 2s 后 SIGKILL

"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class BackgroundTask:
    id: str
    command: str
    started_at: str
    output_path: str           # 绝对路径，stdout+stderr 合并写入
    cwd: str
    popen: subprocess.Popen
    exit_code: Optional[int] = None
    status: str = "running"    # running / done / killed / failed
    finished_at: Optional[str] = None
    notified: bool = False     # 完成通知是否已被 drain 过

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "command": self.command,
            "started_at": self.started_at,
            "output_path": self.output_path,
            "cwd": self.cwd,
            "exit_code": self.exit_code,
            "status": self.status,
            "finished_at": self.finished_at,
            "duration_seconds": self._duration(),
        }

    def _duration(self) -> Optional[float]:
        """命令实际执行耗时（秒）。未结束时返回 None，避免模型把 wall-clock 当耗时。"""
        if not self.finished_at:
            return None
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
            return round((end - start).total_seconds(), 3)
        except (ValueError, TypeError):
            return None


class BackgroundRegistry:
    """进程内后台任务表，BashTool 实例共享。"""

    def __init__(self, output_dir: Optional[Path] = None):
        self._tasks: Dict[str, BackgroundTask] = {}
        self._lock = threading.Lock()
        self._output_dir = Path(output_dir or "./.cbagent/bash_outputs")

    # ---------- 启动 ----------

    def spawn(
        self,
        task_id: str,
        command: str,
        argv: List[str],
        cwd: str,
    ) -> BackgroundTask:
        """启动后台进程。argv 已经是 shell + [wrapped_command] 形式。

        stdout/stderr 都写到同一个文件（合并），避免双 PIPE 协调。
        """
        self._output_dir.mkdir(parents=True, exist_ok=True)
        output_path = (self._output_dir / f"{task_id}.log").resolve()

        # 用二进制写盘，避免 PowerShell encoding 协商
        log_fp = open(output_path, "wb")

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        preexec_fn = None
        if os.name != "nt":
            # 让子进程脱离父进程的进程组，方便 SIGTERM 整组
            preexec_fn = os.setsid  # type: ignore[attr-defined]

        try:
            popen = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log_fp,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                preexec_fn=preexec_fn,
            )
        except Exception:
            log_fp.close()
            raise
        finally:
            # 子进程已继承 fd，父进程关掉自己的副本（避免 ResourceWarning + fd 泄漏）
            try:
                log_fp.close()
            except OSError:
                pass

        task = BackgroundTask(
            id=task_id,
            command=command,
            started_at=datetime.now(timezone.utc).isoformat(),
            output_path=str(output_path),
            cwd=cwd,
            popen=popen,
        )
        with self._lock:
            self._tasks[task_id] = task
        return task

    # ---------- 状态 ----------

    def list(self) -> List[BackgroundTask]:
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            self._refresh(t)
        return tasks

    def get(self, task_id: str) -> Optional[BackgroundTask]:
        with self._lock:
            t = self._tasks.get(task_id)
        if t:
            self._refresh(t)
        return t

    def wait(self, task_id: str, timeout: float = 30.0) -> Optional[BackgroundTask]:
        """阻塞等到结束或超时。"""
        t = self.get(task_id)
        if not t:
            return None
        try:
            t.popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        self._refresh(t)
        return t

    def kill(self, task_id: str) -> Optional[BackgroundTask]:
        t = self.get(task_id)
        if not t:
            return None
        if t.status != "running":
            return t

        try:
            if os.name == "nt":
                # 先发 CTRL_BREAK，给 2 秒优雅退出
                try:
                    os.kill(t.popen.pid, signal.CTRL_BREAK_EVENT)
                except (OSError, AttributeError):
                    pass
                try:
                    t.popen.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    subprocess.run(
                        ["taskkill", "/T", "/F", "/PID", str(t.popen.pid)],
                        capture_output=True,
                    )
            else:
                try:
                    os.killpg(os.getpgid(t.popen.pid), signal.SIGTERM)
                except (OSError, ProcessLookupError):
                    pass
                try:
                    t.popen.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(t.popen.pid), signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
        except Exception:
            pass

        with self._lock:
            t.status = "killed"
            t.finished_at = datetime.now(timezone.utc).isoformat()
        return t

    # ---------- 完成通知 ----------

    def drain_notifications(self) -> List[BackgroundTask]:
        """拿出所有"已完成且未通知过"的任务，标记为已通知。

        AgentRunner 在每轮 LLM think 调用前用一次，把结果作为 system 消息插入。
        """
        out: List[BackgroundTask] = []
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            self._refresh(t)
            if t.status != "running" and not t.notified:
                t.notified = True
                out.append(t)
        return out

    # ---------- 内部 ----------

    def _refresh(self, t: BackgroundTask) -> None:
        if t.status != "running":
            return
        rc = t.popen.poll()
        if rc is None:
            return
        with self._lock:
            t.exit_code = rc
            t.status = "done" if rc == 0 else "failed"
            t.finished_at = datetime.now(timezone.utc).isoformat()


# ========== 全局单例 ==========

_lock = threading.Lock()
_instance: Optional[BackgroundRegistry] = None


def get_background_registry() -> BackgroundRegistry:
    global _instance
    if _instance is None:
        with _lock:
            if _instance is None:
                _instance = BackgroundRegistry()
    return _instance


def reset_background_registry(output_dir: Optional[Path] = None) -> BackgroundRegistry:
    """仅测试用。"""
    global _instance
    with _lock:
        _instance = BackgroundRegistry(output_dir=output_dir)
    return _instance
