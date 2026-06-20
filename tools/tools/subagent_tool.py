"""Subagent tools for delegated Chat Completions runs."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.cancel import CancelToken, get_current_cancel_token
from agent.executor import ToolExecutor
from agent.session import AgentSession
from agent.subagents import (
    ScopedEventBus,
    SubagentDefinition,
    SubagentRegistry,
    SubagentTask,
    SubagentTaskRegistry,
    make_subagent_completed,
    make_subagent_started,
)
from context import MemoryLoader
from tools.tool import Tool, ToolParameter

logger = logging.getLogger(__name__)


SUBAGENT_HARD_DENY = {
    "agent",
    "agent_task",
    "ask_user_question",
    "bash_permission",
    "qqtool",
    "wechattool",
    "send_message_asset",
}


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


class SubagentRunner:
    def __init__(
        self,
        *,
        llm: Any,
        parent_registry: Any,
        parent_event_bus: Any,
        hook_manager: Any = None,
        cwd: Path,
        ctx_enabled: bool = True,
        skill_manager: Any = None,
        bash_prompt_provider: Any = None,
        trace_summarizer: Any = None,
        language: Optional[str] = "Chinese",
        mcp_clients: Any = None,
        message_logger_factory: Optional[Callable[[str], Any]] = None,
        parent_session_id: str = "",
    ) -> None:
        self.llm = llm
        self.parent_registry = parent_registry
        self.parent_event_bus = parent_event_bus
        self.hook_manager = hook_manager
        self.cwd = Path(cwd)
        self.ctx_enabled = ctx_enabled
        self.skill_manager = skill_manager
        self.bash_prompt_provider = bash_prompt_provider
        self.trace_summarizer = trace_summarizer
        self.language = language
        self.mcp_clients = mcp_clients
        self.message_logger_factory = message_logger_factory
        self.parent_session_id = parent_session_id

    def run(
        self,
        *,
        definition: SubagentDefinition,
        subagent_id: str,
        description: str,
        prompt: str,
        run_in_background: bool,
        task_id: Optional[str],
        cancel_token: Optional[CancelToken],
        start_context: str = "",
        output_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        started_at = time.perf_counter()
        scoped_bus = ScopedEventBus(
            self.parent_event_bus,
            subagent_id=subagent_id,
            subagent_type=definition.name,
            description=description,
            task_id=task_id,
            run_in_background=run_in_background,
            parent_session_id=self.parent_session_id,
        )
        self.parent_event_bus.emit(make_subagent_started(
            subagent_id=subagent_id,
            subagent_type=definition.name,
            description=description,
            task_id=task_id,
            run_in_background=run_in_background,
            parent_session_id=self.parent_session_id,
        ))

        child_hook_manager = None
        if self.hook_manager is not None:
            child_hook_manager = self.hook_manager.with_context(
                event_bus=scoped_bus,
                session_id=f"{self.parent_session_id}:{subagent_id}",
                agent_scope="subagent",
                subagent_id=subagent_id,
                subagent_type=definition.name,
                parent_session_id=self.parent_session_id,
                task_id=task_id,
                run_in_background=run_in_background,
            )

        child_registry = self.parent_registry.clone_filtered(
            allow_names=definition.tools,
            deny_names=SUBAGENT_HARD_DENY,
            event_bus=scoped_bus,
        )
        child_executor = ToolExecutor(
            runner=child_registry.execute_tool,
            event_bus=scoped_bus,
            max_workers=4,
            persist_dir=self.cwd / ".cbagent" / "subagent_tool_results",
            hook_manager=child_hook_manager,
        )
        message_logger = None
        if self.message_logger_factory is not None:
            try:
                message_logger = self.message_logger_factory(f"subagent-{definition.name}-{subagent_id}")
            except Exception:
                logger.exception("create subagent message logger failed")

        child_session = AgentSession(
            llm=self.llm,
            registry=child_registry,
            executor=child_executor,
            event_bus=scoped_bus,
            memory_loader=MemoryLoader(cwd=self.cwd) if self.ctx_enabled else None,
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
            pet_manager=None,
            hook_manager=child_hook_manager,
            system_prompt_addendum=definition.system_prompt,
            max_tool_rounds=definition.max_turns,
            memory_writeback_enabled=False,
            is_subagent=True,
        )

        delegated_prompt = self._build_child_prompt(
            description=description,
            prompt=prompt,
            start_context=start_context,
        )
        status = "done"
        content = ""
        is_error = False
        try:
            token = cancel_token or CancelToken()
            content = child_session.chat(delegated_prompt, cancel_token=token)
            if token.is_cancelled() or scoped_bus.cancelled:
                status = "killed"
        except Exception as exc:  # noqa: BLE001
            logger.exception("subagent run failed: subagent_id=%s type=%s", subagent_id, definition.name)
            status = "failed"
            content = f"{type(exc).__name__}: {exc}"
            is_error = True

        rounds_used = scoped_bus.rounds_used
        content = self._fire_stop_hook(
            subagent_id=subagent_id,
            subagent_type=definition.name,
            description=description,
            prompt=prompt,
            task_id=task_id,
            run_in_background=run_in_background,
            status=status,
            content=content,
            rounds_used=rounds_used,
        )
        elapsed = time.perf_counter() - started_at
        self.parent_event_bus.emit(make_subagent_completed(
            subagent_id=subagent_id,
            subagent_type=definition.name,
            description=description,
            status=status,
            content=content,
            task_id=task_id,
            output_path=output_path,
            duration_seconds=elapsed,
            rounds_used=rounds_used,
            is_error=is_error or status in {"failed", "killed"},
        ))
        return {
            "status": status,
            "content": content,
            "rounds_used": rounds_used,
            "duration_seconds": round(elapsed, 3),
            "subagent_id": subagent_id,
            "subagent_type": definition.name,
            "task_id": task_id,
        }

    @staticmethod
    def _build_child_prompt(*, description: str, prompt: str, start_context: str = "") -> str:
        parts = [
            "[Delegated task]",
            f"Description: {description}",
            "",
            prompt.strip(),
        ]
        if start_context.strip():
            parts.extend(["", "[SubagentStart hook context]", start_context.strip()])
        return "\n".join(parts).strip()

    def _fire_stop_hook(
        self,
        *,
        subagent_id: str,
        subagent_type: str,
        description: str,
        prompt: str,
        task_id: Optional[str],
        run_in_background: bool,
        status: str,
        content: str,
        rounds_used: int,
    ) -> str:
        if self.hook_manager is None or not self.hook_manager.has_event("SubagentStop"):
            return content
        manager = self.hook_manager.with_context(
            agent_scope="root",
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            parent_session_id=self.parent_session_id,
            task_id=task_id,
            run_in_background=run_in_background,
        )
        outcome = manager.fire(
            "SubagentStop",
            {
                "description": description,
                "prompt": prompt,
                "subagent_type": subagent_type,
                "status": status,
                "content": content,
                "rounds_used": rounds_used,
            },
            matcher_value=subagent_type,
            round_idx=rounds_used,
        )
        if outcome.additional_context:
            return content + "\n\n[SubagentStop hook context]\n" + outcome.additional_context
        return content


class AgentTool(Tool):
    def __init__(
        self,
        *,
        registry: SubagentRegistry,
        task_registry: SubagentTaskRegistry,
        runner: SubagentRunner,
        hook_manager: Any = None,
        parent_session_id: str = "",
    ) -> None:
        super().__init__(
            name="agent",
            description=(
                "Delegate a focused task to a subagent. Provide a short description, a full prompt, "
                "and optionally subagent_type. Set run_in_background=true for long tasks and use "
                "agent_task to list/wait/output/kill or list available subagent types."
            ),
        )
        self._registry = registry
        self._task_registry = task_registry
        self._runner = runner
        self._hook_manager = hook_manager
        self._parent_session_id = parent_session_id

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="description", type="string", description="Short task description.", required=True),
            ToolParameter(name="prompt", type="string", description="Complete instructions for the subagent.", required=True),
            ToolParameter(name="subagent_type", type="string", description="Registered subagent type. Defaults to general-purpose.", required=False),
            ToolParameter(name="run_in_background", type="boolean", description="Run asynchronously and return a task id.", required=False, default=False),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        return isinstance(parameters, dict) and bool(parameters.get("description")) and bool(parameters.get("prompt"))

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return _json({"error": "description and prompt are required"})

        description = str(parameters.get("description") or "").strip()
        prompt = str(parameters.get("prompt") or "").strip()
        subagent_type = str(parameters.get("subagent_type") or "general-purpose").strip()
        run_in_background = _bool(parameters.get("run_in_background", False))
        subagent_id = f"subagent_{uuid.uuid4().hex[:8]}"

        start = self._fire_start_hook(
            subagent_id=subagent_id,
            subagent_type=subagent_type,
            description=description,
            prompt=prompt,
            run_in_background=run_in_background,
        )
        if start.get("blocked"):
            return _json({
                "status": "blocked",
                "subagent_id": subagent_id,
                "subagent_type": subagent_type,
                "error": start.get("reason") or "SubagentStart hook blocked the run.",
            })
        description = start["description"]
        prompt = start["prompt"]
        subagent_type = start["subagent_type"]
        run_in_background = bool(start["run_in_background"])
        start_context = str(start.get("additional_context") or "")
        definition = self._registry.get(subagent_type)

        if run_in_background:
            task = self._task_registry.spawn(
                subagent_id=subagent_id,
                subagent_type=definition.name,
                description=description,
                prompt=prompt,
                target=lambda t, token: self._run_background(
                    task=t,
                    token=token,
                    definition=definition,
                    description=description,
                    prompt=prompt,
                    start_context=start_context,
                ),
            )
            return _json({
                "status": "background_started",
                "task_id": task.id,
                "subagent_id": subagent_id,
                "subagent_type": definition.name,
                "description": description,
                "output_path": task.output_path,
                "hint": "Use agent_task(action='output' or 'wait', task_id=...) to inspect the result.",
            })

        token = get_current_cancel_token() or CancelToken()
        result = self._runner.run(
            definition=definition,
            subagent_id=subagent_id,
            description=description,
            prompt=prompt,
            run_in_background=False,
            task_id=None,
            cancel_token=token,
            start_context=start_context,
            output_path=None,
        )
        return _json(result)

    def _run_background(
        self,
        *,
        task: SubagentTask,
        token: CancelToken,
        definition: SubagentDefinition,
        description: str,
        prompt: str,
        start_context: str,
    ) -> Dict[str, Any]:
        return self._runner.run(
            definition=definition,
            subagent_id=task.subagent_id,
            description=description,
            prompt=prompt,
            run_in_background=True,
            task_id=task.id,
            cancel_token=token,
            start_context=start_context,
            output_path=task.output_path,
        )

    def _fire_start_hook(
        self,
        *,
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
            parent_session_id=self._parent_session_id,
            task_id=None,
            run_in_background=run_in_background,
        )
        outcome = manager.fire(
            "SubagentStart",
            payload,
            matcher_value=subagent_type,
        )
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
    def __init__(self, *, registry: SubagentRegistry, task_registry: SubagentTaskRegistry) -> None:
        super().__init__(
            name="agent_task",
            description=(
                "Manage background subagent tasks. Actions: list_agents, list, output, wait, kill."
            ),
        )
        self._registry = registry
        self._task_registry = task_registry

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(name="action", type="string", description="list_agents / list / output / wait / kill.", required=True),
            ToolParameter(name="task_id", type="string", description="Required for output/wait/kill.", required=False),
            ToolParameter(name="timeout", type="number", description="wait timeout in seconds, default 30.", required=False, default=30),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        action = parameters.get("action")
        if action not in {"list_agents", "list", "output", "wait", "kill"}:
            return False
        if action in {"output", "wait", "kill"} and not parameters.get("task_id"):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return _json({"error": "invalid parameters"})
        action = str(parameters.get("action"))
        if action == "list_agents":
            return _json({"agents": [d.public_dict() for d in self._registry.list()]})
        if action == "list":
            return _json({"tasks": [t.to_dict() for t in self._task_registry.list()]})

        task_id = str(parameters.get("task_id") or "")
        if action == "output":
            return self._output(task_id)
        if action == "wait":
            timeout = float(parameters.get("timeout", 30) or 30)
            task = self._task_registry.wait(task_id, timeout=timeout)
            return _json({"task": task.to_dict() if task else None})
        if action == "kill":
            task = self._task_registry.kill(task_id)
            return _json({"task": task.to_dict() if task else None})
        return _json({"error": f"unknown action: {action}"})

    def _output(self, task_id: str) -> str:
        task = self._task_registry.get(task_id)
        if task is None:
            return _json({"error": f"task not found: {task_id}"})
        data: Dict[str, Any] = {}
        path = Path(task.output_path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                data = {"read_error": f"{type(exc).__name__}: {exc}"}
        return _json({"task": task.to_dict(), "output": data})


__all__ = [
    "AgentTool",
    "AgentTaskTool",
    "SubagentRunner",
    "SUBAGENT_HARD_DENY",
]
