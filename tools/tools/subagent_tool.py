"""子代理委派与任务管理工具。"""

from __future__ import annotations

import inspect
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.cancel import CancelToken, get_current_cancel_token
from agent.events import SubagentProgress
from agent.executor import ToolExecutor
from agent.session import AgentSession
from context import MemoryLoader
from subagent.context import get_current_parent_session_id
from subagent.event_bridge import ScopedEventBus, make_subagent_completed, make_subagent_started
from subagent.manager import SubagentTaskManager
from subagent.models import DEFAULT_SUBAGENT_TYPE, SubagentDefinition, SubagentTask
from subagent.permissions import ALWAYS_DENIED_TOOLS, SubagentExecutionPolicy
from subagent.registry import SubagentRegistry
from tools.tool import Tool, ToolParameter
from tools.tools.bash_session import (
    BashSession,
    reset_session_override,
    set_session_override,
)
from tools.tools.local_search import reset_search_ignore_dirs, set_search_ignore_dirs


logger = logging.getLogger(__name__)


SUBAGENT_HARD_DENY = set(ALWAYS_DENIED_TOOLS)


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _owner_session_id() -> str:
    """从当前工具调用上下文解析父会话，缺失时使用稳定兜底值。"""

    return get_current_parent_session_id() or "runtime-main"


class SubagentRunner:
    """为一个已注册任务创建隔离的 AgentSession 并执行委派提示。"""

    def __init__(
        self,
        *,
        llm: Any,
        parent_registry: Any,
        parent_event_bus: Any,
        task_manager: SubagentTaskManager,
        hook_manager: Any = None,
        cwd: Path,
        ctx_enabled: bool = True,
        skill_manager: Any = None,
        bash_prompt_provider: Any = None,
        trace_summarizer: Any = None,
        language: Optional[str] = "Chinese",
        mcp_clients: Any = None,
        message_logger_factory: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.llm = llm
        self.parent_registry = parent_registry
        self.parent_event_bus = parent_event_bus
        self.task_manager = task_manager
        self.hook_manager = hook_manager
        self.cwd = Path(cwd).resolve()
        self.ctx_enabled = ctx_enabled
        self.skill_manager = skill_manager
        self.bash_prompt_provider = bash_prompt_provider
        self.trace_summarizer = trace_summarizer
        self.language = language
        self.mcp_clients = mcp_clients
        self.message_logger_factory = message_logger_factory
        self.task_manager.subscribe_events(self._emit_task_event)

    def run(
        self,
        *,
        task: SubagentTask,
        definition: SubagentDefinition,
        description: str,
        prompt: str,
        cancel_token: CancelToken,
        start_context: str = "",
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        scoped_bus = ScopedEventBus(
            self.parent_event_bus,
            subagent_id=task.subagent_id,
            subagent_type=definition.name,
            description=description,
            task_id=task.id,
            run_in_background=task.run_in_background,
            parent_session_id=task.owner_session_id,
            task_manager=self.task_manager,
        )
        delegated_prompt = self._build_child_prompt(
            description=description,
            prompt=prompt,
            start_context=start_context,
        )
        status = "completed"
        content = ""
        bash_session_token = None
        search_ignore_token = None
        message_logger = None
        try:
            child_hook_manager = None
            if self.hook_manager is not None:
                child_hook_manager = self.hook_manager.with_context(
                    event_bus=scoped_bus,
                    session_id=f"{task.owner_session_id}:{task.subagent_id}",
                    agent_scope="subagent",
                    subagent_id=task.subagent_id,
                    subagent_type=definition.name,
                    parent_session_id=task.owner_session_id,
                    task_id=task.id,
                    run_in_background=task.run_in_background,
                )

            # 文件、搜索和 Bash 工具历史上都从进程级 BashSession 解析相对路径。
            # 为每个子代理绑定独立上下文，避免主 Agent 或其它子代理切换目录后造成串扰。
            child_bash_session = BashSession(initial_cwd=str(self.cwd), is_subagent=True)
            task_runtime_dir = (
                self.cwd / ".cbagent" / "subagent_tool_results" / task.id
            ).resolve()
            clone_method = self.parent_registry.clone_filtered
            clone_kwargs: Dict[str, Any] = {
                "allow_names": definition.tools,
                "deny_names": SUBAGENT_HARD_DENY,
                "event_bus": scoped_bus,
            }
            try:
                parameters = inspect.signature(clone_method).parameters
                accepts_extra = any(
                    item.kind == inspect.Parameter.VAR_KEYWORD
                    for item in parameters.values()
                )
                if accepts_extra or "bash_session" in parameters:
                    clone_kwargs["bash_session"] = child_bash_session
                if accepts_extra or "bash_output_dir" in parameters:
                    clone_kwargs["bash_output_dir"] = task_runtime_dir / "bash_outputs"
            except (TypeError, ValueError):
                # 某些扩展对象无法提供 Python 签名；保持旧版最小参数集合。
                pass
            clone_session_token = set_session_override(child_bash_session)
            try:
                child_registry = clone_method(**clone_kwargs)
            finally:
                reset_session_override(clone_session_token)
            # 旧扩展注册表可能不接受 bash_output_dir，但若仍暴露标准 get_tool，
            # 运行器可以在克隆后补上任务私有输出目录，保持会话隔离语义。
            get_child_tool = getattr(child_registry, "get_tool", None)
            if callable(get_child_tool):
                child_bash = get_child_tool("bash")
                if child_bash is not None and hasattr(child_bash, "_output_dir"):
                    child_bash._output_dir = (task_runtime_dir / "bash_outputs").resolve()
            execution_policy = SubagentExecutionPolicy(
                definition,
                self.cwd,
                allowed_internal_paths=(task_runtime_dir,),
            )
            child_executor = ToolExecutor(
                runner=child_registry.execute_tool,
                event_bus=scoped_bus,
                max_workers=4,
                persist_dir=task_runtime_dir / "tool_results",
                hook_manager=child_hook_manager,
            )

            if self.message_logger_factory is not None:
                try:
                    message_logger = self.message_logger_factory(
                        f"subagent-{definition.name}-{task.subagent_id}"
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("创建子代理消息日志失败")

            child_session = AgentSession(
                llm=self.llm,
                registry=child_registry,
                executor=child_executor,
                event_bus=scoped_bus,
                memory_loader=MemoryLoader(cwd=self.cwd) if self.ctx_enabled else None,
                # SkillManager 可以提供技能索引，但角色权限仍会在执行器层拦截越权工具。
                skill_manager=self.skill_manager,
                bash_prompt_provider=self.bash_prompt_provider,
                ctx_enabled=self.ctx_enabled,
                history_window=8,
                messages_snapshot_hook=None,
                session_store=None,
                trace_summarizer=self.trace_summarizer,
                message_logger=message_logger,
                language=self.language,
                mcp_clients=self.mcp_clients,
                hook_manager=child_hook_manager,
                system_prompt_addendum=definition.system_prompt,
                max_tool_rounds=definition.max_turns,
                memory_writeback_enabled=False,
                is_subagent=True,
                runtime_session_id=f"{task.owner_session_id}:{task.id}",
                tool_execution_policy=execution_policy,
                runtime_message_provider=lambda: self.task_manager.drain_messages(task.id),
            )

            bash_session_token = set_session_override(child_bash_session)
            search_ignore_token = set_search_ignore_dirs({".cbagent"})
            content = str(child_session.chat(delegated_prompt, cancel_token=cancel_token) or "")
            if cancel_token.is_cancelled() or scoped_bus.cancelled:
                status = "cancelled"
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "子代理运行失败: subagent_id=%s type=%s",
                task.subagent_id,
                definition.name,
            )
            status = "cancelled" if cancel_token.is_cancelled() else "failed"
            content = f"{type(exc).__name__}: {exc}"
        finally:
            if search_ignore_token is not None:
                reset_search_ignore_dirs(search_ignore_token)
            if bash_session_token is not None:
                reset_session_override(bash_session_token)
            if message_logger is not None:
                close_logger = getattr(message_logger, "close", None)
                if callable(close_logger):
                    try:
                        close_logger()
                    except Exception:
                        logger.exception("关闭子代理消息日志失败: task_id=%s", task.id)

        if task.status == "orphaned":
            status = "orphaned"
            content = task.error or content
        elif cancel_token.is_cancelled() or scoped_bus.cancelled:
            status = "cancelled"
        rounds_used = scoped_bus.rounds_used
        if status != "orphaned":
            try:
                content = self._fire_stop_hook(
                    task=task,
                    description=description,
                    prompt=prompt,
                    status=status,
                    content=content,
                    rounds_used=rounds_used,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("执行 SubagentStop hook 失败: task_id=%s", task.id)
                if status != "cancelled":
                    status = "failed"
                hook_error = f"SubagentStop hook 失败: {type(exc).__name__}: {exc}"
                content = f"{content}\n\n{hook_error}".strip()
        if task.status == "orphaned":
            status = "orphaned"
            content = task.error or content
        elif cancel_token.is_cancelled() or scoped_bus.cancelled:
            status = "cancelled"
        elapsed = time.perf_counter() - started_at
        return {
            "status": status,
            "content": content,
            "rounds_used": rounds_used,
            "duration_seconds": round(elapsed, 3),
            "subagent_id": task.subagent_id,
            "subagent_type": definition.name,
            "task_id": task.id,
        }

    def _emit_task_event(self, task: SubagentTask, event: Dict[str, Any]) -> None:
        """把管理器已持久化事件转换成唯一的父级 UI 事件流。"""

        event_type = str(event.get("type") or "")
        show_current_tool = bool(task.current_tool_name) and task.phase in {
            "running_tool",
            "cancelling",
            "shutdown",
        }
        suppress_historical_tool = task.phase in {"cancelling", "shutdown"} and not show_current_tool
        if show_current_tool:
            progress_tool_name = task.current_tool_name
            progress_tool_call_id = task.current_tool_call_id
            progress_arguments = dict(task.current_tool_arguments)
        elif suppress_historical_tool:
            progress_tool_name = ""
            progress_tool_call_id = ""
            progress_arguments = {}
        else:
            progress_tool_name = str(event.get("tool_name") or "")
            progress_tool_call_id = str(event.get("tool_call_id") or "")
            progress_arguments = (
                dict(event.get("arguments"))
                if isinstance(event.get("arguments"), dict)
                else {}
            )
        if event_type == "queued" or (event_type == "started" and not task.run_in_background):
            self.emit_started(
                task,
                task.description,
                status=task.status,
                phase=task.phase,
            )
            return
        if task.is_terminal() and event_type in {"completed", "failed", "cancelled", "orphaned"}:
            self.parent_event_bus.emit(make_subagent_completed(
                subagent_id=task.subagent_id,
                subagent_type=task.subagent_type,
                description=task.description,
                status=task.status,
                content=task.result or task.error,
                task_id=task.id,
                parent_session_id=task.owner_session_id,
                output_path=task.output_path,
                duration_seconds=float(task.duration_seconds() or 0.0),
                rounds_used=task.rounds_used,
                is_error=task.status in {"failed", "cancelled", "orphaned"},
            ))
            return
        self.parent_event_bus.emit(SubagentProgress(
            subagent_id=task.subagent_id,
            subagent_type=task.subagent_type,
            task_id=task.id,
            parent_session_id=task.owner_session_id,
            status=str(event.get("status") or task.status),
            phase=str(event.get("phase") or task.phase),
            message=str(event.get("message") or ""),
            event_seq=int(event.get("seq") or 0),
            round_idx=int(event.get("round_idx") or task.current_round or 0),
            tool_name=progress_tool_name,
            tool_call_id=progress_tool_call_id,
            arguments_preview=progress_arguments,
            tool_uses=int(event.get("tool_uses") or task.tool_uses or 0),
            active_tool_count=int(
                event.get("active_tool_count") or len(task.active_tool_calls) or 0
            ),
            total_tokens=int(event.get("total_tokens") or task.total_tokens or 0),
        ))

    def emit_started(
        self,
        task: SubagentTask,
        description: str,
        *,
        status: str,
        phase: str,
    ) -> None:
        """广播任务进入队列或前台开始，保证每个 task_id 只出现一个面板。"""

        self.parent_event_bus.emit(make_subagent_started(
            subagent_id=task.subagent_id,
            subagent_type=task.subagent_type,
            description=description,
            task_id=task.id,
            run_in_background=task.run_in_background,
            parent_session_id=task.owner_session_id,
            status=status,
            phase=phase,
        ))

    @staticmethod
    def _build_child_prompt(*, description: str, prompt: str, start_context: str = "") -> str:
        parts = [
            "[委派任务]",
            f"任务说明：{description}",
            "",
            prompt.strip(),
        ]
        if start_context.strip():
            parts.extend(["", "[SubagentStart hook 补充上下文]", start_context.strip()])
        return "\n".join(parts).strip()

    def _fire_stop_hook(
        self,
        *,
        task: SubagentTask,
        description: str,
        prompt: str,
        status: str,
        content: str,
        rounds_used: int,
    ) -> str:
        if self.hook_manager is None or not self.hook_manager.has_event("SubagentStop"):
            return content
        manager = self.hook_manager.with_context(
            agent_scope="root",
            subagent_id=task.subagent_id,
            subagent_type=task.subagent_type,
            parent_session_id=task.owner_session_id,
            task_id=task.id,
            run_in_background=task.run_in_background,
        )
        outcome = manager.fire(
            "SubagentStop",
            {
                "description": description,
                "prompt": prompt,
                "subagent_type": task.subagent_type,
                "status": status,
                "content": content,
                "rounds_used": rounds_used,
            },
            matcher_value=task.subagent_type,
            round_idx=rounds_used,
        )
        if outcome.additional_context:
            return content + "\n\n[SubagentStop hook 补充上下文]\n" + outcome.additional_context
        return content


class AgentTool(Tool):
    def __init__(
        self,
        *,
        registry: SubagentRegistry,
        task_manager: SubagentTaskManager,
        runner: SubagentRunner,
        hook_manager: Any = None,
    ) -> None:
        super().__init__(
            name="agent",
            description=(
                "把独立任务委派给专用子代理。独立工作应设置 run_in_background=true，"
                "同一轮可启动多个后台任务并继续处理不重叠工作。使用 agent_task inspect "
                "查看实时工具进度，仅在关键路径需要结果时使用 wait。"
            ),
        )
        self._registry = registry
        self._task_manager = task_manager
        self._runner = runner
        self._hook_manager = hook_manager

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="description", type="string", description="简短任务说明。", required=True),
            ToolParameter(name="prompt", type="string", description="给子代理的完整任务指令。", required=True),
            ToolParameter(name="subagent_type", type="string", description="已注册角色，默认 general。", required=False),
            ToolParameter(
                name="run_in_background",
                type="boolean",
                description="是否后台并行执行；只有下一步必须依赖结果时才设为 false。",
                required=False,
                default=True,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        description = parameters.get("description")
        prompt = parameters.get("prompt")
        return (
            isinstance(description, str)
            and bool(description.strip())
            and isinstance(prompt, str)
            and bool(prompt.strip())
        )

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return _json({"error": "description 和 prompt 为必填项"})

        owner_session_id = _owner_session_id()
        description = str(parameters.get("description") or "").strip()
        prompt = str(parameters.get("prompt") or "").strip()
        requested_type = str(parameters.get("subagent_type") or DEFAULT_SUBAGENT_TYPE).strip()
        run_in_background = _bool(parameters.get("run_in_background", True))
        subagent_id = f"subagent_{uuid.uuid4().hex[:8]}"

        start = self._fire_start_hook(
            owner_session_id=owner_session_id,
            subagent_id=subagent_id,
            subagent_type=requested_type,
            description=description,
            prompt=prompt,
            run_in_background=run_in_background,
        )
        if start.get("blocked"):
            return _json({
                "status": "blocked",
                "subagent_id": subagent_id,
                "subagent_type": requested_type,
                "error": start.get("reason") or "SubagentStart hook 已阻止运行",
            })
        description = str(start["description"])
        prompt = str(start["prompt"])
        run_in_background = bool(start["run_in_background"])
        start_context = str(start.get("additional_context") or "")
        try:
            # 允许用户在进程运行期间新增或调整 .cbagent/agents/*.md。
            self._registry.refresh()
            definition = self._registry.get(str(start["subagent_type"]))
        except ValueError as exc:
            return _json({"status": "failed", "error": str(exc), "subagent_id": subagent_id})

        def target(task: SubagentTask, token: CancelToken) -> Dict[str, Any]:
            return self._runner.run(
                task=task,
                definition=definition,
                description=description,
                prompt=prompt,
                cancel_token=token,
                start_context=start_context,
            )

        if run_in_background:
            try:
                task = self._task_manager.spawn(
                    owner_session_id=owner_session_id,
                    subagent_id=subagent_id,
                    subagent_type=definition.name,
                    description=description,
                    prompt=prompt,
                    target=target,
                )
            except Exception as exc:  # noqa: BLE001
                return _json({"status": "failed", "error": f"{type(exc).__name__}: {exc}"})
            return _json({
                "status": "background_started",
                "task_id": task.id,
                "subagent_id": subagent_id,
                "subagent_type": definition.name,
                "description": description,
                "output_path": task.output_path,
                "hint": "继续处理不重叠工作；需要实时状态时使用 agent_task(action='inspect', task_id=...)。",
            })

        token = get_current_cancel_token() or CancelToken()
        task, result = self._task_manager.run_foreground(
            owner_session_id=owner_session_id,
            subagent_id=subagent_id,
            subagent_type=definition.name,
            description=description,
            prompt=prompt,
            target=target,
            cancel_token=token,
        )
        return _json({**result, "task": task.to_dict()})

    def _fire_start_hook(
        self,
        *,
        owner_session_id: str,
        subagent_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
        run_in_background: bool,
    ) -> Dict[str, Any]:
        payload = {
            "description": description,
            "prompt": prompt,
            "subagent_type": subagent_type,
            "run_in_background": run_in_background,
        }
        if self._hook_manager is None or not self._hook_manager.has_event("SubagentStart"):
            return {**payload, "blocked": False, "additional_context": ""}
        manager = self._hook_manager.with_context(
            agent_scope="root",
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            parent_session_id=owner_session_id,
            task_id=None,
            run_in_background=run_in_background,
        )
        outcome = manager.fire("SubagentStart", payload, matcher_value=subagent_type)
        if outcome.blocked or outcome.stop:
            return {**payload, "blocked": True, "reason": outcome.block_reason}
        updated = outcome.updated_input if isinstance(outcome.updated_input, dict) else {}
        return {
            "description": str(updated.get("description", description)),
            "prompt": str(updated.get("prompt", prompt)),
            "subagent_type": str(updated.get("subagent_type", subagent_type)),
            "run_in_background": _bool(updated.get("run_in_background", run_in_background)),
            "blocked": False,
            "additional_context": outcome.additional_context,
        }


class AgentTaskTool(Tool):
    def __init__(self, *, registry: SubagentRegistry, task_manager: SubagentTaskManager) -> None:
        super().__init__(
            name="agent_task",
            description=(
                "管理当前会话的子代理任务。操作：list_agents、list、inspect、output、"
                "wait、message、cancel；kill 是 cancel 的兼容别名。inspect 是非阻塞实时查询。"
            ),
        )
        self._registry = registry
        self._task_manager = task_manager

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", description="任务管理操作。", required=True),
            ToolParameter(name="task_id", type="string", description="单个任务 ID。", required=False),
            ToolParameter(name="task_ids", type="array", description="wait 可同时等待的任务 ID。", required=False),
            ToolParameter(name="timeout", type="number", description="wait 超时秒数。", required=False, default=30),
            ToolParameter(name="cursor", type="number", description="inspect 的上次事件游标。", required=False, default=0),
            ToolParameter(name="limit", type="number", description="inspect 最大事件数。", required=False, default=50),
            ToolParameter(name="message", type="string", description="message 操作投递的补充指令。", required=False),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        action = parameters.get("action")
        allowed = {"list_agents", "list", "inspect", "output", "wait", "message", "cancel", "kill"}
        if not isinstance(action, str) or action not in allowed:
            return False
        task_id = parameters.get("task_id")
        if action in {"inspect", "output", "message", "cancel", "kill"} and (
            not isinstance(task_id, str) or not task_id.strip()
        ):
            return False
        message = parameters.get("message")
        if action == "message" and (
            not isinstance(message, str) or not message.strip()
        ):
            return False
        if action == "wait":
            task_ids = parameters.get("task_ids")
            if task_ids is not None and not isinstance(task_ids, list):
                return False
            if not (isinstance(task_id, str) and task_id.strip()) and not task_ids:
                return False
        for numeric_name in ("timeout", "cursor", "limit"):
            value = parameters.get(numeric_name)
            if value is not None and not isinstance(value, (int, float)):
                return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return _json({"error": "agent_task 参数无效"})
        owner_session_id = _owner_session_id()
        action = str(parameters.get("action"))

        if action == "list_agents":
            self._registry.refresh()
            return _json({
                "agents": [definition.public_dict() for definition in self._registry.list()],
                "definition_errors": self._registry.errors(),
            })
        if action == "list":
            return _json({
                "tasks": [task.to_dict() for task in self._task_manager.list(owner_session_id)]
            })

        task_id = str(parameters.get("task_id") or "")
        if action == "inspect":
            data = self._task_manager.inspect(
                task_id,
                owner_session_id=owner_session_id,
                cursor=int(parameters.get("cursor") or 0),
                limit=int(parameters.get("limit") or 50),
            )
            return _json(data or {"error": f"任务不存在或不属于当前会话: {task_id}"})
        if action == "output":
            return self._output(task_id, owner_session_id)
        if action == "wait":
            raw_ids = parameters.get("task_ids")
            task_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
            if task_id and task_id not in task_ids:
                task_ids.append(task_id)
            tasks = self._task_manager.wait(
                task_ids,
                owner_session_id=owner_session_id,
                timeout=min(300.0, max(0.0, float(parameters.get("timeout", 30) or 30))),
            )
            return _json({"tasks": [task.to_dict() for task in tasks]})
        if action == "message":
            task = self._task_manager.send_message(
                task_id,
                owner_session_id=owner_session_id,
                message=str(parameters.get("message") or ""),
            )
            return _json({
                "task": task.to_dict() if task else None,
                "error": None if task else "任务不存在、已结束或不属于当前会话",
            })
        if action in {"cancel", "kill"}:
            task = self._task_manager.cancel(task_id, owner_session_id=owner_session_id)
            return _json({
                "task": task.to_dict() if task else None,
                "error": None if task else "任务不存在或不属于当前会话",
            })
        return _json({"error": f"未知操作: {action}"})

    def _output(self, task_id: str, owner_session_id: str) -> str:
        task = self._task_manager.get(task_id, owner_session_id)
        if task is None:
            return _json({"error": f"任务不存在或不属于当前会话: {task_id}"})
        content = task.result or task.error
        path = Path(task.output_path)
        if path.is_symlink():
            return _json({
                "task": task.to_dict(),
                "read_error": "拒绝读取符号链接形式的子代理结果文件",
            })
        if path.exists():
            try:
                disk_content = path.read_text(encoding="utf-8")
                if disk_content or not content:
                    content = disk_content
            except Exception as exc:  # noqa: BLE001
                return _json({"task": task.to_dict(), "read_error": f"{type(exc).__name__}: {exc}"})
        return _json({"task": task.to_dict(), "output": content})


__all__ = ["AgentTaskTool", "AgentTool", "SUBAGENT_HARD_DENY", "SubagentRunner"]
