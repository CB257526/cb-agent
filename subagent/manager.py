"""子代理任务生命周期、进度状态和持久化管理。"""

from __future__ import annotations

import atexit
from collections import deque
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from agent.cancel import CancelToken
from agent.events import (
    Cancelled,
    Done,
    Error,
    RoundEnd,
    RoundStart,
    TokenUsage,
    ToolComplete,
    ToolStart,
)
from subagent.models import (
    ACTIVE_TASK_STATES,
    TERMINAL_TASK_STATES,
    SubagentTask,
    clip_text,
    utc_now,
)


logger = logging.getLogger(__name__)

TaskTarget = Callable[[SubagentTask, CancelToken], Dict[str, Any]]
TaskEventListener = Callable[[SubagentTask, Dict[str, Any]], None]

_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}

_SENSITIVE_TEXT_PATTERNS = (
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|password|secret|authorization)"
        r"\s*[:=]\s*([^\s'\"]+)"
    ),
)


class SubagentTaskManager:
    """统一管理前台和后台子代理任务。

    后台任务使用固定数量 daemon worker，超过并发上限的任务留在内存队列中。所有运行态
    都先写入任务快照，再广播到 UI，主 Agent 因而可以随时通过游标读取一致状态。
    """

    def __init__(
        self,
        output_dir: Path,
        *,
        max_workers: int = 4,
        max_pending_tasks: int = 32,
        recent_event_limit: int = 80,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.max_workers = max(1, int(max_workers))
        self.max_pending_tasks = max(self.max_workers, int(max_pending_tasks))
        self.recent_event_limit = max(20, int(recent_event_limit))
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._tasks: Dict[str, SubagentTask] = {}
        self._queue: queue.Queue[Optional[Tuple[SubagentTask, TaskTarget]]] = queue.Queue()
        # 已取消的排队项仍会留在 Queue 中等待 worker 跳过，因此单独计数，防止
        # 反复“提交后立即取消”绕过 max_pending_tasks 并无限堆积占位项。
        self._queued_entries = 0
        self._event_listeners: List[TaskEventListener] = []
        self._workers: List[threading.Thread] = []
        self._closed = False
        self._load_snapshots()
        for index in range(self.max_workers):
            worker = threading.Thread(
                target=self._worker_loop,
                name=f"cb-subagent-worker-{index + 1}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        atexit.register(self.shutdown)

    # ---------- 任务创建与执行 ----------

    def subscribe_events(self, listener: TaskEventListener) -> None:
        """订阅已写盘任务事件；监听器同步收到最新任务对象和事件副本。"""

        with self._lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def spawn(
        self,
        *,
        owner_session_id: str,
        subagent_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
        target: TaskTarget,
        on_queued: Optional[Callable[[SubagentTask], None]] = None,
    ) -> SubagentTask:
        """提交后台任务，达到并发上限时由线程池排队。"""

        with self._lock:
            self._ensure_open()
            active_count = sum(1 for task in self._tasks.values() if task.status in ACTIVE_TASK_STATES)
            if active_count >= self.max_pending_tasks or self._queued_entries >= self.max_pending_tasks:
                raise RuntimeError(
                    f"后台子代理任务已达到上限 {self.max_pending_tasks}，请等待或取消已有任务"
                )
            task = self._create_task(
                owner_session_id=owner_session_id,
                subagent_id=subagent_id,
                subagent_type=subagent_type,
                description=description,
                prompt=prompt,
                run_in_background=True,
            )
            self._tasks[task.id] = task
            self._record_event_locked(task, "queued", "任务已进入后台执行队列", status="queued")
            if on_queued is not None:
                try:
                    on_queued(task)
                except Exception:
                    logger.exception("广播子代理排队事件失败: task_id=%s", task.id)
            self._queued_entries += 1
            self._queue.put_nowait((task, target))
            return task

    def _worker_loop(self) -> None:
        """固定后台 worker：从队列取任务，退出时由 None 哨兵结束。"""

        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                task, target = item
                with self._lock:
                    self._queued_entries = max(0, self._queued_entries - 1)
                self._execute_task(task, target)
            finally:
                self._queue.task_done()

    def run_foreground(
        self,
        *,
        owner_session_id: str,
        subagent_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
        target: TaskTarget,
        cancel_token: CancelToken,
    ) -> Tuple[SubagentTask, Dict[str, Any]]:
        """注册并同步运行前台任务，取消令牌直接继承父回合。"""

        with self._lock:
            self._ensure_open()
            task = self._create_task(
                owner_session_id=owner_session_id,
                subagent_id=subagent_id,
                subagent_type=subagent_type,
                description=description,
                prompt=prompt,
                run_in_background=False,
                cancel_token=cancel_token,
            )
            self._tasks[task.id] = task
        result = self._execute_task(task, target)
        return task, result

    def _create_task(
        self,
        *,
        owner_session_id: str,
        subagent_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
        run_in_background: bool,
        cancel_token: Optional[CancelToken] = None,
    ) -> SubagentTask:
        task_id = f"subagent_{uuid.uuid4().hex[:10]}"
        snapshot_path = self.output_dir / f"{task_id}.json"
        events_path = self.output_dir / f"{task_id}.events.jsonl"
        output_path = self.output_dir / f"{task_id}.result.txt"
        return SubagentTask(
            id=task_id,
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            owner_session_id=str(owner_session_id or "runtime-main"),
            description=description,
            prompt=prompt,
            output_path=str(output_path),
            snapshot_path=str(snapshot_path),
            events_path=str(events_path),
            started_at=utc_now(),
            run_in_background=run_in_background,
            cancel_token=cancel_token or CancelToken(),
        )

    def _execute_task(self, task: SubagentTask, target: TaskTarget) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        try:
            with self._lock:
                # 已取消的排队项或 shutdown 后的 orphaned 项仍可能被 Queue 取出；
                # 它们是不可逆终态，worker 只能跳过，不能再次运行 target。
                if task.is_terminal():
                    return {
                        "status": task.status,
                        "content": task.result or task.error,
                        "rounds_used": task.rounds_used,
                        "task_id": task.id,
                    }
                if task.cancel_requested or task.cancel_token.is_cancelled():
                    task.status = "cancelled"
                    task.phase = "cancelled"
                    self._finish_task_locked(task)
                    self._record_event_locked(task, "cancelled", "任务在开始前已取消", status="cancelled")
                    return {"status": "cancelled", "content": "", "rounds_used": 0}
                task.status = "running"
                task.phase = "starting"
                self._record_event_locked(task, "started", "子代理开始运行", status="running")

            raw_result = target(task, task.cancel_token)
            result = raw_result if isinstance(raw_result, dict) else {
                "status": "completed",
                "content": "" if raw_result is None else str(raw_result),
            }
            content = str(result.get("content") or "")
            rounds_used = int(result.get("rounds_used") or task.rounds_used or 0)
            requested_status = str(result.get("status") or "completed")
            if requested_status == "done":
                requested_status = "completed"

            with self._lock:
                # shutdown 超时后任务已被明确标记 orphaned，迟到的线程结果不能
                # 再把生命周期改写成 completed，避免 UI 和磁盘出现状态倒退。
                if task.status == "orphaned":
                    return {
                        **result,
                        "status": "orphaned",
                        "content": task.result or task.error,
                        "rounds_used": task.rounds_used,
                        "task_id": task.id,
                    }
                task.result = content
                task.rounds_used = rounds_used
                if task.cancel_requested or task.cancel_token.is_cancelled() or requested_status in {"killed", "cancelled"}:
                    task.status = "cancelled"
                    task.phase = "cancelled"
                elif requested_status == "failed":
                    task.status = "failed"
                    task.phase = "failed"
                else:
                    task.status = "completed"
                    task.phase = "completed"
                self._finish_task_locked(task)
                self._record_event_locked(
                    task,
                    task.status,
                    "子代理任务已结束",
                    status=task.status,
                    result_preview=clip_text(task.result, 500),
                )
            return {
                **result,
                "status": task.status,
                "content": content,
                "rounds_used": rounds_used,
                "task_id": task.id,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("后台子代理执行失败: task_id=%s", task.id)
            with self._lock:
                if task.status == "orphaned":
                    return {
                        "status": "orphaned",
                        "content": task.error,
                        "rounds_used": task.rounds_used,
                        "task_id": task.id,
                    }
                task.error = f"{type(exc).__name__}: {exc}"
                if task.cancel_requested or task.cancel_token.is_cancelled():
                    task.status = "cancelled"
                    task.phase = "cancelled"
                    event_message = f"任务取消期间停止: {task.error}"
                else:
                    task.status = "failed"
                    task.phase = "failed"
                    event_message = task.error
                self._finish_task_locked(task)
                self._record_event_locked(
                    task,
                    task.status,
                    event_message,
                    status=task.status,
                )
            return {
                "status": task.status,
                "content": task.error,
                "rounds_used": task.rounds_used,
                "task_id": task.id,
            }

    def _finish_task_locked(self, task: SubagentTask) -> None:
        task.finished_at = utc_now()
        task.updated_at = task.finished_at
        task.heartbeat_at = task.finished_at
        task.current_tool_name = ""
        task.current_tool_call_id = ""
        task.current_tool_arguments = {}
        task.current_tool_started_at = None
        task.active_tool_calls.clear()
        self._write_result_locked(task)
        self._condition.notify_all()

    # ---------- 子会话事件与实时快照 ----------

    def record_child_event(self, task_id: str, event: Any) -> Optional[Dict[str, Any]]:
        """把子会话事件归一成任务进度并持久化。

        这是子代理 AgentSession 在执行过程中**唯一**的进度上报入口：
        ScopedEventBus 收到子代理发出的事件后会回调到这里，由本方法
        完成"事件 → 任务状态更新 + 事件流持久化"的全部工作。

        Args:
            task_id: 子代理任务 ID（即 SubagentTask.id，对应 agent_task
                工具的 task_id）。若该任务不存在或已进入终态（completed
                / failed / cancelled / orphaned），则忽略事件并返回 None。
            event: 子会话产出的事件对象，支持以下类型：

                * ``RoundStart``   - 一轮 LLM 调用开始
                * ``RoundEnd``     - 一轮 LLM 调用结束
                * ``ToolStart``    - 工具调用开始
                * ``ToolComplete`` - 工具调用结束
                * ``TokenUsage``   - 本轮 token 消耗
                * ``Error``        - 运行期错误（不一定是终态）
                * ``Cancelled``    - 取消流程开始
                * ``Done``         - 子代理最终回答已生成

                未识别的类型会被静默忽略，返回 None。

        Returns:
            归一化后的事件字典（已分配 ``event_seq``），可由
            :meth:`inspect` 通过 cursor 拉取；任务不存在/已终态或事件
            类型不识别时返回 None。

        各事件对任务状态的影响（与 ``cancel_requested`` 联动）：

        ============== =============================================================
        事件            状态/字段变化
        ============== =============================================================
        RoundStart     status=running/cancelling，phase=thinking/cancelling，
                       current_round = event.round_idx
        RoundEnd       phase = cancelling | tool_results_ready (有工具调用)
                       | finishing (无工具调用)
        ToolStart      status=waiting_tool/cancelling，phase=running_tool，
                       记录 current_tool_name/call_id/arguments/started_at，
                       写入 active_tool_calls，tool_uses += 1
        ToolComplete   从 active_tool_calls 移除该 call，更新
                       last_tool_name/status/duration；若有其他活跃工具
                       则把 current_tool_* 切到最新的那个，否则回到
                       running/thinking 并清空 current_tool_*
        TokenUsage     total_tokens += event.total_tokens
        Error          phase = error（注意：不一定终止 AgentSession，
                       最终 status 由 runner 返回值决定）
        Cancelled      status = cancelling，phase = cancelling
        Done           rounds_used = event.rounds_used
        ============== =============================================================

        线程安全：
            整个方法体在 :attr:`_lock`（manager 级互斥锁）下执行；进入
            后会进一步在 :meth:`_record_event_locked` 里加 ``task.lock``
            （任务级可重入锁），保证任务查找、状态更新、事件序号分配
            对并发事件是原子的。

        副作用：
            * 改写 ``task`` 的 ``status`` / ``phase`` / ``current_*`` /
              ``last_*`` / ``event_seq`` / ``heartbeat_at`` 等字段；
            * 通过 :meth:`_record_event_locked` 增量追加到
              ``task.events_path``（JSONL 格式），供父进程 inspect 拉取；
            * 触发 ScopedEventBus 转发到父总线，UI 端可实时显示进度。
        """

        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.is_terminal():
                return None

            if isinstance(event, RoundStart):
                task.status = "cancelling" if task.cancel_requested else "running"
                task.phase = "cancelling" if task.cancel_requested else "thinking"
                task.current_round = int(event.round_idx or 0)
                return self._record_event_locked(
                    task, "round_started", f"第 {task.current_round} 轮模型调用开始",
                    status=task.status, round_idx=task.current_round,
                )
            if isinstance(event, RoundEnd):
                task.phase = (
                    "cancelling"
                    if task.cancel_requested
                    else "tool_results_ready" if event.has_tool_calls else "finishing"
                )
                return self._record_event_locked(
                    task, "round_ended", f"第 {event.round_idx} 轮结束",
                    status=task.status, round_idx=int(event.round_idx or 0),
                )
            if isinstance(event, ToolStart):
                task.status = "cancelling" if task.cancel_requested else "waiting_tool"
                task.phase = "cancelling" if task.cancel_requested else "running_tool"
                task.current_tool_name = str(event.name or "")
                task.current_tool_call_id = str(event.call_id or "")
                task.current_tool_arguments = _redact_arguments(event.arguments)
                task.current_tool_started_at = utc_now()
                active_key = task.current_tool_call_id or f"tool-{task.tool_uses + 1}"
                task.active_tool_calls[active_key] = {
                    "name": task.current_tool_name,
                    "call_id": task.current_tool_call_id,
                    "arguments": dict(task.current_tool_arguments),
                    "started_at": task.current_tool_started_at,
                }
                task.tool_uses += 1
                return self._record_event_locked(
                    task,
                    "tool_started",
                    f"工具 {task.current_tool_name} 开始执行",
                    status=task.status,
                    round_idx=int(event.round_idx or 0),
                    tool_name=task.current_tool_name,
                    tool_call_id=task.current_tool_call_id,
                    arguments=dict(task.current_tool_arguments),
                )
            if isinstance(event, ToolComplete):
                task.last_tool_name = str(event.name or "")
                task.last_tool_status = "error" if event.is_error else "completed"
                task.last_tool_duration = float(event.duration_seconds or 0.0)
                completed_key = str(event.call_id or "")
                removed = task.active_tool_calls.pop(completed_key, None) if completed_key else None
                if removed is None:
                    for active_key, active_tool in list(task.active_tool_calls.items()):
                        if str(active_tool.get("name") or "") == task.last_tool_name:
                            task.active_tool_calls.pop(active_key, None)
                            break
                if task.active_tool_calls:
                    latest = next(reversed(task.active_tool_calls.values()))
                    task.status = "cancelling" if task.cancel_requested else "waiting_tool"
                    task.phase = "cancelling" if task.cancel_requested else "running_tool"
                    task.current_tool_name = str(latest.get("name") or "")
                    task.current_tool_call_id = str(latest.get("call_id") or "")
                    task.current_tool_arguments = dict(latest.get("arguments") or {})
                    task.current_tool_started_at = latest.get("started_at")
                else:
                    task.status = "cancelling" if task.cancel_requested else "running"
                    task.phase = "cancelling" if task.cancel_requested else "thinking"
                    task.current_tool_name = ""
                    task.current_tool_call_id = ""
                    task.current_tool_arguments = {}
                    task.current_tool_started_at = None
                return self._record_event_locked(
                    task,
                    "tool_completed",
                    f"工具 {task.last_tool_name} {'执行失败' if event.is_error else '执行完成'}",
                    status=task.status,
                    round_idx=int(event.round_idx or 0),
                    tool_name=task.last_tool_name,
                    tool_call_id=str(event.call_id or ""),
                    is_error=bool(event.is_error),
                    duration_seconds=task.last_tool_duration,
                )
            if isinstance(event, TokenUsage):
                task.total_tokens += int(event.total_tokens or 0)
                return self._record_event_locked(
                    task,
                    "token_usage",
                    f"本轮使用 {int(event.total_tokens or 0)} tokens",
                    status=task.status,
                    round_idx=int(event.round_idx or 0),
                    total_tokens=task.total_tokens,
                )
            if isinstance(event, Error):
                task.phase = "error"
                return self._record_event_locked(
                    task,
                    "error",
                    f"{event.where}: {clip_text(event.message, 300)}",
                    # Error 事件不一定终止 AgentSession；最终状态由 runner 返回值决定。
                    status="error",
                    round_idx=int(event.round_idx or 0),
                )
            if isinstance(event, Cancelled):
                task.status = "cancelling"
                task.phase = "cancelling"
                return self._record_event_locked(
                    task,
                    "cancelling",
                    f"正在取消: {event.where}",
                    status="cancelling",
                    round_idx=int(event.round_idx or 0),
                )
            if isinstance(event, Done):
                task.rounds_used = int(event.rounds_used or task.rounds_used or 0)
                return self._record_event_locked(
                    task,
                    "answer_ready",
                    "子代理最终回答已生成",
                    status=task.status,
                    rounds_used=task.rounds_used,
                )
            return None

    def _record_event_locked(
        self,
        task: SubagentTask,
        event_type: str,
        message: str,
        **fields: Any,
    ) -> Dict[str, Any]:
        with task.lock:
            task.event_seq += 1
            task.updated_at = utc_now()
            task.heartbeat_at = task.updated_at
            event = {
                "seq": task.event_seq,
                "type": event_type,
                "message": clip_text(message, 600),
                "status": str(fields.pop("status", task.status)),
                "phase": task.phase,
                "timestamp": task.updated_at,
                "tool_uses": task.tool_uses,
                "total_tokens": task.total_tokens,
                "active_tool_count": len(task.active_tool_calls),
                **fields,
            }
            task.recent_events.append(event)
            if len(task.recent_events) > self.recent_event_limit:
                del task.recent_events[: len(task.recent_events) - self.recent_event_limit]
            self._append_event_locked(task, event)
            self._write_snapshot_locked(task)
            for listener in list(self._event_listeners):
                try:
                    listener(task, dict(event))
                except Exception:
                    logger.exception(
                        "子代理任务事件监听器执行失败: task_id=%s listener=%r",
                        task.id,
                        listener,
                    )
            return dict(event)

    # ---------- 查询、通知和消息邮箱 ----------

    def list(self, owner_session_id: Optional[str] = None) -> List[SubagentTask]:
        with self._lock:
            tasks = list(self._tasks.values())
        if owner_session_id is not None:
            tasks = [task for task in tasks if task.owner_session_id == owner_session_id]
        return sorted(tasks, key=lambda task: task.started_at)

    def adopt_legacy_tasks(self, owner_session_id: str) -> int:
        """把旧版没有父会话字段的任务一次性归入主会话。"""

        adopted = 0
        with self._lock:
            for task in self._tasks.values():
                if task.owner_session_id != "legacy-main":
                    continue
                task.owner_session_id = owner_session_id
                self._record_event_locked(
                    task,
                    "legacy_adopted",
                    "旧版子代理任务已迁移到当前主会话",
                    status=task.status,
                )
                adopted += 1
        return adopted

    def get(self, task_id: str, owner_session_id: Optional[str] = None) -> Optional[SubagentTask]:
        with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return None
        if owner_session_id is not None and task.owner_session_id != owner_session_id:
            return None
        return task

    def inspect(
        self,
        task_id: str,
        *,
        owner_session_id: str,
        cursor: int = 0,
        limit: int = 50,
    ) -> Optional[Dict[str, Any]]:
        task = self.get(task_id, owner_session_id)
        if task is None:
            return None
        cursor = max(0, int(cursor))
        limit = min(200, max(1, int(limit)))
        with task.lock:
            events = [dict(item) for item in task.recent_events if int(item.get("seq") or 0) > cursor]
            if len(events) > limit:
                events = events[-limit:]
            return {
                "task": task.to_dict(),
                "events": events,
                "next_cursor": task.event_seq,
                "truncated": bool(events and int(events[0].get("seq") or 0) > cursor + 1),
            }

    def wait(
        self,
        task_ids: Sequence[str],
        *,
        owner_session_id: str,
        timeout: float = 30.0,
    ) -> List[SubagentTask]:
        """等待任一目标结束；超时后返回所有目标的最新状态。"""

        tasks = [self.get(task_id, owner_session_id) for task_id in task_ids]
        found = [task for task in tasks if task is not None]
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while found and not any(task.is_terminal() for task in found):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(timeout=remaining)
            return found

    def cancel(self, task_id: str, *, owner_session_id: str) -> Optional[SubagentTask]:
        with self._lock:
            task = self.get(task_id, owner_session_id)
            if task is None:
                return None
            if task.is_terminal():
                return task
            task.cancel_requested = True
            task.cancel_token.cancel()
            if task.status == "queued":
                task.status = "cancelled"
                task.phase = "cancelled"
                self._finish_task_locked(task)
                self._record_event_locked(
                    task,
                    "cancelled",
                    "排队中的子代理任务已取消",
                    status="cancelled",
                )
                return task
            task.status = "cancelling"
            task.phase = "cancelling"
            self._record_event_locked(task, "cancelling", "已请求取消子代理任务", status=task.status)
            return task

    def cancel_owner(self, owner_session_id: str) -> List[SubagentTask]:
        """取消某个父会话仍在运行或排队的全部任务。"""

        cancelled: List[SubagentTask] = []
        for task in self.list(owner_session_id):
            if task.is_terminal():
                continue
            updated = self.cancel(task.id, owner_session_id=owner_session_id)
            if updated is not None:
                cancelled.append(updated)
        return cancelled

    def send_message(self, task_id: str, *, owner_session_id: str, message: str) -> Optional[SubagentTask]:
        text = str(message or "").strip()
        if not text:
            raise ValueError("message 不能为空")
        with self._lock:
            task = self.get(task_id, owner_session_id)
            if task is None or task.is_terminal():
                return None
            with task.lock:
                task.inbox.append(clip_text(text, 4000))
            self._record_event_locked(task, "message_queued", "父 Agent 已补充任务上下文", status=task.status)
            return task

    def drain_messages(self, task_id: str) -> List[str]:
        task = self.get(task_id)
        if task is None:
            return []
        with task.lock:
            messages = list(task.inbox)
            task.inbox.clear()
        if messages:
            with self._lock:
                self._record_event_locked(task, "message_delivered", "补充上下文已交付子代理", status=task.status)
        return messages

    def drain_parent_updates(self, owner_session_id: str, *, max_events: int = 24) -> str:
        """合并父会话尚未消费的进度，生成下一轮模型可读通知。"""

        blocks: List[str] = []
        remaining = max(1, int(max_events))
        with self._lock:
            tasks = [task for task in self._tasks.values() if task.owner_session_id == owner_session_id]
            tasks.sort(key=lambda item: item.started_at)
            for task in tasks:
                with task.lock:
                    pending = [
                        dict(item)
                        for item in task.recent_events
                        if int(item.get("seq") or 0) > task.parent_cursor
                    ]
                    if not pending:
                        continue
                    # 高频工具事件只保留最近几条，但最终状态永远保留。
                    selected = pending[-min(4, remaining):]
                    terminal = [item for item in pending if item.get("status") in TERMINAL_TASK_STATES]
                    if terminal and terminal[-1] not in selected:
                        selected.append(terminal[-1])
                    task.parent_cursor = task.event_seq
                    remaining = max(0, remaining - len(selected))
                    snapshot = task.to_dict()
                    lines = [
                        f'<subagent-update task_id="{task.id}" type="{task.subagent_type}" '
                        f'status="{task.status}" phase="{task.phase}">',
                        f"description: {_context_literal(task.description)}",
                    ]
                    for item in selected:
                        lines.append(
                            f"- seq={item.get('seq')} type={item.get('type')} "
                            f"message={_context_literal(item.get('message'))}"
                        )
                    current_tool = snapshot.get("current_tool")
                    if current_tool:
                        lines.append(
                            f"current_tool: {current_tool.get('name')} "
                            f"arguments={_context_literal(current_tool.get('arguments') or {})}"
                        )
                    if task.is_terminal():
                        lines.append(f"output_path: {_context_literal(task.output_path)}")
                        if task.error:
                            lines.append(f"error: {_context_literal(task.error)}")
                        elif task.result:
                            lines.append(f"result_preview: {_context_literal(clip_text(task.result, 800))}")
                    lines.append("</subagent-update>")
                    blocks.append("\n".join(lines))
                    self._write_snapshot_locked(task)
                if remaining <= 0:
                    break
        if not blocks:
            return ""
        return (
            "[Subagent runtime updates]\n"
            "以下是后台子代理自上一轮模型调用后的增量状态。不要无条件等待；"
            "若任务仍运行，请继续处理不重叠工作。\n\n"
            + "\n\n".join(blocks)
        )

    # ---------- 持久化与关闭 ----------

    def _load_snapshots(self) -> None:
        for path in sorted(self.output_dir.glob("subagent_*.json")):
            try:
                if path.is_symlink():
                    logger.warning("跳过符号链接形式的子代理快照: %s", path)
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                task = SubagentTask.from_dict(payload, snapshot_path=path)
                # 快照内容可能被手工修改，任务 ID 和所有持久化路径必须由实际文件名
                # 重新派生，不能信任 JSON 中可指向工作区外的任意路径。
                task.id = path.stem
                task.snapshot_path = str(path.resolve())
                task.output_path = str(path.with_suffix(".result.txt").resolve())
                task.events_path = str(path.with_suffix(".events.jsonl").resolve())
                events_path = Path(task.events_path)
                if events_path.is_symlink():
                    events_path.unlink()
                elif events_path.exists():
                    self._restore_event_tail(task, events_path)
                result_path = Path(task.output_path)
                if result_path.is_symlink():
                    result_path.unlink()
                if (task.result or task.error) and not result_path.exists():
                    self._write_result_locked(task)
                elif not task.result and result_path.exists():
                    try:
                        task.result = result_path.read_text(encoding="utf-8")
                    except Exception:
                        logger.exception("恢复子代理最终输出失败: %s", result_path)
                invalid_status = (
                    task.status not in ACTIVE_TASK_STATES
                    and task.status not in TERMINAL_TASK_STATES
                )
                if task.status in ACTIVE_TASK_STATES or invalid_status:
                    previous_status = task.status
                    task.status = "orphaned"
                    task.phase = "orphaned"
                    task.error = (
                        f"快照包含未知任务状态 {previous_status!r}，已按 orphaned 隔离"
                        if invalid_status
                        else "进程重启时任务仍处于运行态，无法安全恢复执行"
                    )
                    with self._lock:
                        self._finish_task_locked(task)
                        self._record_event_locked(task, "orphaned", task.error, status="orphaned")
                elif task.is_terminal():
                    task.finished_at = task.finished_at or task.updated_at or utc_now()
                    task.current_tool_name = ""
                    task.current_tool_call_id = ""
                    task.current_tool_arguments = {}
                    task.current_tool_started_at = None
                    task.active_tool_calls.clear()
                self._tasks[task.id] = task
            except Exception:  # noqa: BLE001
                logger.exception("恢复子代理任务快照失败: %s", path)

    def _restore_event_tail(self, task: SubagentTask, path: Path) -> None:
        """从 JSONL 恢复最近事件和最大序号，处理“事件已写、快照未替换”的崩溃窗口。"""

        tail = deque(maxlen=self.recent_event_limit)
        max_seq = task.event_seq
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(item, dict):
                        continue
                    try:
                        seq = int(item.get("seq") or 0)
                    except (TypeError, ValueError):
                        continue
                    tail.append(dict(item))
                    max_seq = max(max_seq, seq)
        except OSError:
            logger.exception("恢复子代理事件日志失败: %s", path)
            return
        if tail:
            merged: Dict[int, Dict[str, Any]] = {}
            for item in task.recent_events:
                if not isinstance(item, dict):
                    continue
                try:
                    merged[int(item.get("seq") or 0)] = dict(item)
                except (TypeError, ValueError):
                    continue
            for item in tail:
                merged[int(item.get("seq") or 0)] = dict(item)
            task.recent_events = [
                merged[seq]
                for seq in sorted(merged)[-self.recent_event_limit:]
            ]
        task.event_seq = max_seq

    def _append_event_locked(self, task: SubagentTask, event: Dict[str, Any]) -> None:
        path = Path(task.events_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def _write_snapshot_locked(self, task: SubagentTask) -> None:
        path = Path(task.snapshot_path)
        # 完整最终回答只写 result.txt；快照保留预览，避免长回答在两个 JSON 文件中
        # 重复占用空间。prompt 仍保留用于本地审计和问题复现。
        payload = task.to_dict(include_prompt=True, include_result=False)
        payload["parent_cursor"] = task.parent_cursor
        payload["recent_events"] = [dict(item) for item in task.recent_events]
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, default=str))

    def _write_result_locked(self, task: SubagentTask) -> None:
        _atomic_write_text(Path(task.output_path), task.result or task.error or "")

    def shutdown(self, timeout: float = 2.0) -> None:
        """取消所有未结束任务并有限等待，避免退出时无限阻塞。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            active = [task for task in self._tasks.values() if not task.is_terminal()]
            for task in active:
                task.cancel_requested = True
                task.cancel_token.cancel()
                if task.status == "queued":
                    task.status = "cancelled"
                    task.phase = "cancelled"
                    self._finish_task_locked(task)
                    self._record_event_locked(
                        task,
                        "cancelled",
                        "应用退出，排队中的子代理任务已取消",
                        status="cancelled",
                    )
                else:
                    task.status = "cancelling"
                    task.phase = "shutdown"
                    self._record_event_locked(
                        task,
                        "shutdown",
                        "应用退出，正在取消子代理",
                        status="cancelling",
                    )
        for _worker in self._workers:
            self._queue.put_nowait(None)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for worker in self._workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(timeout=remaining)
        with self._lock:
            for task in active:
                if not task.is_terminal():
                    task.status = "orphaned"
                    task.phase = "orphaned"
                    task.error = "应用退出前任务未能在等待时间内结束"
                    self._finish_task_locked(task)
                    self._record_event_locked(task, "orphaned", task.error, status="orphaned")
            self._condition.notify_all()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("子代理任务管理器已经关闭")


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件写入后原子替换，避免进程中断留下半个 JSON。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.replace(temp, path)


def _redact_arguments(arguments: Any) -> Dict[str, Any]:
    """生成适合展示和持久化的工具参数摘要。"""

    if not isinstance(arguments, dict):
        return {}
    result: Dict[str, Any] = {}
    for key, value in arguments.items():
        normalized = str(key).lower()
        if any(sensitive in normalized for sensitive in _SENSITIVE_KEYS):
            result[str(key)] = "[已脱敏]"
            continue
        if isinstance(value, str):
            result[str(key)] = clip_text(_redact_text(value), 300)
        elif isinstance(value, (int, float, bool)) or value is None:
            result[str(key)] = value
        else:
            result[str(key)] = clip_text(json.dumps(value, ensure_ascii=False, default=str), 300)
    return result


def _redact_text(value: str) -> str:
    """对命令行和普通字符串中常见的凭据形态做二次脱敏。"""

    text = value
    for pattern in _SENSITIVE_TEXT_PATTERNS:
        text = pattern.sub(lambda match: f"{match.group(1)} [已脱敏]", text)
    return text


def _context_literal(value: Any) -> str:
    """把运行态值编码成单个 JSON 字面量，并转义提醒标签分隔符。"""

    encoded = json.dumps(value, ensure_ascii=False, default=str)
    return encoded.replace("<", "\\u003c").replace(">", "\\u003e")


__all__ = ["SubagentTaskManager", "TaskEventListener", "TaskTarget"]
