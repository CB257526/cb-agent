"""工具执行调度器

把 LLM 一轮返回的 tool_calls 合理调度执行：
- **全部是只读**（file_read / glob / grep / ls / search / bash_task list/output/wait）→ 线程池并发
- **任一非只读**（bash / file_edit / file_write / todo / bash_permission / MCP）→ 串行

为什么不更激进地"按工具组合"细判：
- bash 命令的"是否只读"是命令文本动态决定的（cat 只读 vs rm 写），但 cb-agent
  内部 bash_classify 判定逻辑分散，调度层不应反向耦合 bash 内部
- file_read + file_edit/file_write 看似可"并发不同文件"，但写入工具的 stale check
  依赖 ReadStateRegistry 共享态，同时跑的并发收益不抵复杂度
- 简单"全只读才并发"覆盖 80% 的真并发收益场景（一轮多个 grep / glob / search /
  memory.search），剩余仍按原顺序串行执行，零回归风险

ContextVars 跨线程传播：
- AgentSession 调用前用 set_current_cancel_token 绑 token
- 这里用 contextvars.copy_context() 抓快照，在 worker thread ctx.run(...)
- 工具内部 get_current_cancel_token() 拿到的是发起时的 token，hermes 同款做法
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from agent.cancel import (
    CancellationReason,
    CancelToken,
    ToolCancelledError,
)
from agent.event_bus import EventBus
from agent.events import ToolComplete, ToolStart
from agent.platforms.permissions import (
    check_platform_tool_permission,
    permission_denied_payload,
)
from agent.result_cap import cap_single_result
from agent.tool_execution import (
    ToolCancellationMode,
    ToolEffectState,
    ToolExecutionContext,
    ToolTerminalStatus,
    ToolTimeoutPolicy,
)

logger = logging.getLogger(__name__)


# ========== 并发判定 ==========


# 纯读工具白名单：执行不写任何全局态、不调用副作用 API。
# 命中本集合的工具，组合并发是安全的。
READ_ONLY_TOOLS: Set[str] = {
    "file_read",
    "glob",
    "grep",
    "ls",
    "search",
    "rag",                 # 仅 query
    "memory_search",
}

# 纯读但需要二次判别 action 的工具
READ_ONLY_IF_ACTION: Dict[str, Set[str]] = {
    # bash_task: list/output/wait 是读，kill 是写
    "bash_task": {"list", "output", "wait"},
    # bash_permission: list/check 是读，grant/revoke 是写
    "bash_permission": {"list", "check"},
    # memory: search/stats 是读，store/delete 是写
    "memory": {"search", "stats", "list"},
}


def _is_read_only(tool_name: str, arguments: Dict[str, Any]) -> bool:
    if tool_name in READ_ONLY_TOOLS:
        return True
    if tool_name in READ_ONLY_IF_ACTION:
        action = (arguments or {}).get("action", "")
        return action in READ_ONLY_IF_ACTION[tool_name]
    return False


def should_parallelize(tool_calls: List[Dict[str, Any]]) -> bool:
    """判断这一批 tool_calls 是否能安全并发执行。

    tool_call 形如：
        {"id": "...", "type": "function",
         "function": {"name": "...", "arguments": "<json str>"}}

    规则：≤1 个直接 False（无意义）；全部纯读 True；否则 False。
    """
    if len(tool_calls) <= 1:
        return False
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        args_str = tc.get("function", {}).get("arguments", "{}")
        try:
            args = json.loads(args_str) if args_str else {}
        except (json.JSONDecodeError, TypeError):
            # arguments 解析失败 → 当成"可能写"，整批串行
            return False
        if not _is_read_only(name, args):
            return False
    return True


# ========== 单工具执行 ==========


# runner 可以是旧的二参数函数，也可以是新的三参数 ToolRegistry.execute_tool。
ToolRunner = Callable[..., str]


@dataclass
class ToolCallResult:
    """单条工具执行结果。"""
    call_id: str
    name: str
    arguments: Dict[str, Any]
    result: str               # 工具的 run() 返回（通常 JSON 字符串）
    duration_seconds: float
    is_error: bool
    status: ToolTerminalStatus = ToolTerminalStatus.COMPLETED
    effect_state: ToolEffectState = ToolEffectState.COMPLETED
    cancel_reason: Optional[CancellationReason] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None

    def __post_init__(self) -> None:
        # 兼容旧测试和扩展代码中只传 is_error 的构造方式。
        if self.is_error and self.status == ToolTerminalStatus.COMPLETED:
            self.status = ToolTerminalStatus.FAILED
        self.is_error = self.status != ToolTerminalStatus.COMPLETED


def _parse_arguments(raw: str) -> Dict[str, Any]:
    """把工具的 arguments JSON 字符串解析成 dict，失败时返回空 dict。"""
    if not raw:
        return {}
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _hook_blocked_payload(tool_name: str, arguments: Dict[str, Any], reason: str) -> str:
    """PreToolUse hook 阻止工具时回灌给模型的结构化结果。

    沿用 permission_denied_payload 的思路：工具没真正执行，但仍按 tool calling
    协议回灌一条 tool 消息；结构化 JSON 让模型稳定识别这是 hook 拦截而非异常。
    """
    payload = {
        "hook_blocked": True,
        "error": reason or "PreToolUse hook 阻止了该工具调用",
        "tool": tool_name,
        "hint": "该操作被本地 hooks 配置拦截。如需放行，请调整 .cbagent/hooks.json。",
    }
    if tool_name == "bash":
        payload["command"] = str((arguments or {}).get("command") or "")[:500]
    return json.dumps(payload, ensure_ascii=False)


def _append_hook_context(result: str, context: str) -> str:
    """把 PostToolUse hook 注入的额外上下文追加进工具结果。

    优先把 context 合进 result 的 JSON 结构（加 _hook_context 字段）；result 不是
    JSON 对象时退化成纯文本拼接。两种方式都让模型在 tool 消息里看到 hook 提示，
    AgentSession 回灌逻辑零改动。
    """
    text = (result or "").strip()
    if text.startswith("{"):
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                obj["_hook_context"] = context
                return json.dumps(obj, ensure_ascii=False)
        except json.JSONDecodeError:
            pass
    return f"{result}\n\n[hook 注入上下文]\n{context}"


def _stringify_tool_result(result: Any) -> str:
    """Normalize arbitrary Tool.run return values before events/history see them."""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception:
        return str(result)


def _result_declares_error(result: str) -> bool:
    """读取工具或 result cap 返回的结构化错误标记。"""
    try:
        payload = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return False
    return bool(
        isinstance(payload, dict)
        and (
            payload.get("is_error") is True
            or payload.get("result_cap_persist_failed") is True
        )
    )


# ========== 调度器 ==========


class ToolExecutor:
    """工具调度器。AgentSession 一轮 tool_calls 来一批，调它跑完拿结果。"""

    def __init__(
        self,
        runner: ToolRunner,
        event_bus: Optional[EventBus] = None,
        max_workers: int = 4,
        persist_dir: Optional[Path] = None,
        hook_manager: Optional[Any] = None,
        timeout_policy: Optional[ToolTimeoutPolicy] = None,
    ) -> None:
        """
        Args:
            runner: 接受 (tool_name, args_dict) 返回 str 的可调用。
                    通常是 ToolRegistry.execute_tool。
            event_bus: 可选事件总线，发 ToolStart / ToolComplete
            max_workers: 并发线程池大小上限。一批超过这个数仍并发，但被池内排队。
            persist_dir: 工具结果超限时的持久化目录。默认 .cbagent/tool_results/
            hook_manager: 可选 HookManager，在工具执行前后触发 PreToolUse /
                          PostToolUse hook。None 表示不启用 hooks（零回归）。
        """
        self._runner = runner
        self._bus = event_bus
        self._max_workers = max_workers
        self._persist_dir = persist_dir or Path(os.getcwd()) / ".cbagent" / "tool_results"
        self._hook_manager = hook_manager
        self._timeout_policy = timeout_policy or ToolTimeoutPolicy()
        # 线程池由执行器持有，避免 with ThreadPoolExecutor 在取消路径上隐式等待。
        # 调用本身仍会等待 blocking 工具安全结束，不会把未知副作用线程遗留后台。
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cb-tool",
        )
        runner_owner = getattr(runner, "__self__", None)
        self._profile_resolver = getattr(runner_owner, "get_execution_profile", None)
        # 真实 ToolRegistry 同时提供执行画像和三参数入口。测试或扩展代码可能用
        # MagicMock 替换 execute_tool；此时只保留二参数兼容，避免 *args 签名误判。
        self._runner_accepts_context = callable(self._profile_resolver)

    def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int = 0,
        cancel_token: Optional[CancelToken] = None,
        execution_policy: Optional[Any] = None,
        result_callback: Optional[Callable[[ToolCallResult], None]] = None,
        start_callback: Optional[Callable[[ToolExecutionContext, Dict[str, Any]], None]] = None,
        turn_id: str = "",
    ) -> List[ToolCallResult]:
        """执行一批 tool_calls，返回**保持 tool_calls 输入顺序**的结果列表。

        消息回灌的顺序必须跟模型 tool_calls 里一致（OpenAI 协议要求每个
        tool_call_id 对应一个 tool 消息），这里返回结果保序，方便 AgentSession
        直接 zip。

        已运行的工具通过子取消上下文收到即时通知；尚未运行的调用生成
        cancelled_before_start 终态。普通 blocking 工具无法安全杀线程，因此取消后
        仍等待它返回；Bash/MCP 等 runtime 工具会在上下文回调中主动清理。

        execution_policy（Plan Mode 服务端工具管控）：
          传入一个可调用策略对象（如 PlanExecutionPolicy），在 _run_one 中
          每个工具执行前先 check()。如果策略拒绝，直接返回 denied_result()
          而不调用真正的工具 runner。这比 prompt 层面的"请勿使用写入工具"
          更可靠——prompt 可能被模型忽略，服务端拒绝则是硬保证。
        """
        if not tool_calls:
            return []
        tool_names = [
            tc.get("function", {}).get("name", "")
            for tc in tool_calls
        ]
        logger.info(
            "executor start: round=%s calls=%s tools=%s",
            round_idx,
            len(tool_calls),
            tool_names,
        )

        # submit 前最后一次窗口：已 cancel 就直接全部占位
        if cancel_token is not None and cancel_token.is_cancelled():
            logger.info("executor cancelled before submit: round=%s calls=%s", round_idx, len(tool_calls))
            results = [
                self._cancelled_before_start(tc, cancel_token, round_idx)
                for tc in tool_calls
            ]
            for result in results:
                self._notify_result_callback(result_callback, result)
            return results

        if should_parallelize(tool_calls):
            logger.info("executor mode: parallel round=%s calls=%s", round_idx, len(tool_calls))
            results = self._execute_parallel(
                tool_calls,
                round_idx,
                cancel_token,
                execution_policy,
                result_callback,
                start_callback,
                turn_id,
            )
        else:
            logger.info("executor mode: serial round=%s calls=%s", round_idx, len(tool_calls))
            results = self._execute_serial(
                tool_calls,
                round_idx,
                cancel_token,
                execution_policy,
                result_callback,
                start_callback,
                turn_id,
            )

        # 每条结果在发事件和写终态前已经执行统一上限；此处不再做会造成同一
        # call_id 二次终态写入的批量改写。
        return results

    # ---------- 串行 ----------

    def _execute_serial(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int,
        cancel_token: Optional[CancelToken],
        execution_policy: Optional[Any],
        result_callback: Optional[Callable[[ToolCallResult], None]],
        start_callback: Optional[Callable[[ToolExecutionContext, Dict[str, Any]], None]],
        turn_id: str,
    ) -> List[ToolCallResult]:
        """串行执行一批 tool_calls。

        每执行一个工具前检查 cancel_token；已取消的 tool_calls 填入占位结果，
        保证 OpenAI 协议每个 tool_call_id 都有对应的 tool 消息回灌。
        """
        results: List[ToolCallResult] = []
        for tc in tool_calls:
            if cancel_token is not None and cancel_token.is_cancelled():
                logger.info(
                    "executor skipped tool after cancel: round=%s name=%s",
                    round_idx,
                    tc.get("function", {}).get("name", ""),
                )
                result = self._cancelled_before_start(tc, cancel_token, round_idx)
                results.append(result)
                self._notify_result_callback(result_callback, result)
                continue
            result = self._run_one(
                tc, round_idx, execution_policy, cancel_token, start_callback, turn_id
            )
            results.append(result)
            self._notify_result_callback(result_callback, result)
        return results

    # ---------- 并行 ----------

    def _execute_parallel(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int,
        cancel_token: Optional[CancelToken],
        execution_policy: Optional[Any],
        result_callback: Optional[Callable[[ToolCallResult], None]],
        start_callback: Optional[Callable[[ToolExecutionContext, Dict[str, Any]], None]],
        turn_id: str,
    ) -> List[ToolCallResult]:
        # 每个 worker 拿一份 ctx 副本：同一个 contextvars.Context 不能并发
        # 多次 ctx.run（会抛 "context already entered"）。我们在主线程抓
        # 状态，给每个 worker 各 copy_context() 一份独立 ctx。
        results: List[Optional[ToolCallResult]] = [None] * len(tool_calls)
        futs: Dict[Future, int] = {
            self._pool.submit(
                contextvars.copy_context().run,
                self._run_one, tc, round_idx, execution_policy,
                cancel_token, start_callback, turn_id,
            ): i
            for i, tc in enumerate(tool_calls)
        }
        pending = set(futs)
        first_base_error: Optional[BaseException] = None
        first_base_error_traceback = None
        while pending:
            done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            if cancel_token is not None and cancel_token.is_cancelled():
                # 只取消尚未进入 worker 的任务；已运行任务由其子上下文负责清理。
                for future in tuple(pending):
                    if future.cancel():
                        idx = futs[future]
                        result = self._cancelled_before_start(
                            tool_calls[idx], cancel_token, round_idx
                        )
                        results[idx] = result
                        pending.remove(future)
                        self._notify_result_callback(result_callback, result)
            for future in done:
                idx = futs[future]
                try:
                    result = future.result()
                except BaseException as exc:
                    # 仍继续收集其他已启动调用，确保成功工具先完成终态持久化。
                    if first_base_error is None:
                        first_base_error = exc
                        first_base_error_traceback = exc.__traceback__
                    continue
                results[idx] = result
                self._notify_result_callback(result_callback, result)
        if first_base_error is not None:
            raise first_base_error.with_traceback(first_base_error_traceback)
        return [result for result in results if result is not None]

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _notify_result_callback(
        callback: Optional[Callable[[ToolCallResult], None]],
        result: ToolCallResult,
    ) -> None:
        """通知调用方某个工具已经完成。

        这个回调只服务运行中检查点等旁路持久化；不能影响工具执行主流程。
        """
        if callback is None:
            return
        try:
            callback(result)
        except Exception:  # noqa: BLE001
            logger.exception(
                "工具结果回调失败: name=%s call_id=%s",
                result.name,
                result.call_id,
            )

    # ---------- 单条 ----------

    def _cap_model_visible_result(self, result: Any, *, call_id: str, name: str) -> str:
        """在结果进入事件、检查点或消息历史前执行统一模型可见上限。"""
        rendered = _stringify_tool_result(result)
        capped, persisted = cap_single_result(
            rendered, call_id, name, self._persist_dir,
        )
        if persisted:
            logger.info(
                "tool result persisted: name=%s call_id=%s dir=%s",
                name, call_id, self._persist_dir,
            )
        return capped

    def _execution_policy_denial(
        self,
        *,
        execution_policy: Any,
        name: str,
        args: Dict[str, Any],
        call_id: str,
        round_idx: int,
    ) -> Optional[ToolCallResult]:
        """执行策略拒绝时构造完整协议结果；允许时返回 None。"""

        try:
            allowed, reason = execution_policy.check(name, args)
        except Exception as e:  # noqa: BLE001
            # 策略检查自身异常时保守拒绝，避免策略缺陷直接放大成越权执行。
            logger.exception("execution policy failed: name=%s call_id=%s", name, call_id)
            allowed, reason = False, f"execution policy error: {type(e).__name__}: {e}"
        if allowed:
            return None

        result = self._cap_model_visible_result(
            execution_policy.denied_result(name, args, reason or "tool denied"),
            call_id=call_id,
            name=name,
        )
        logger.warning(
            "tool denied by execution policy: round=%s name=%s call_id=%s reason=%s",
            round_idx,
            name,
            call_id,
            reason,
        )
        if self._bus is not None:
            self._bus.emit(ToolStart(
                call_id=call_id, name=name, arguments=args,
                round_idx=round_idx,
            ))
            self._bus.emit(ToolComplete(
                call_id=call_id, name=name, result=result,
                duration_seconds=0.0, is_error=True,
                round_idx=round_idx,
            ))
        return ToolCallResult(
            call_id=call_id,
            name=name,
            arguments=args,
            result=result,
            duration_seconds=0.0,
            is_error=True,
            status=ToolTerminalStatus.FAILED,
            effect_state=ToolEffectState.NONE,
        )

    def _platform_permission_denial(
        self,
        *,
        name: str,
        args: Dict[str, Any],
        call_id: str,
        round_idx: int,
    ) -> Optional[ToolCallResult]:
        """通讯平台权限拒绝时构造工具结果；允许时返回 None。"""

        permission = check_platform_tool_permission(name, args)
        if not permission.denied:
            return None

        result = self._cap_model_visible_result(
            permission_denied_payload(name, args, permission),
            call_id=call_id,
            name=name,
        )
        logger.warning(
            "tool denied by platform permission: round=%s name=%s call_id=%s reason=%s",
            round_idx,
            name,
            call_id,
            permission.reason,
        )
        if self._bus is not None:
            self._bus.emit(ToolComplete(
                call_id=call_id, name=name, result=result,
                duration_seconds=0.0, is_error=True,
                round_idx=round_idx,
            ))
        return ToolCallResult(
            call_id=call_id, name=name, arguments=args,
            result=result, duration_seconds=0.0, is_error=True,
            status=ToolTerminalStatus.FAILED,
            effect_state=ToolEffectState.NONE,
        )

    def _run_one(
        self,
        tool_call: Dict[str, Any],
        round_idx: int,
        execution_policy: Optional[Any] = None,
        cancel_token: Optional[CancelToken] = None,
        start_callback: Optional[Callable[[ToolExecutionContext, Dict[str, Any]], None]] = None,
        turn_id: str = "",
    ) -> ToolCallResult:
        call_id = tool_call.get("id", "") or f"call_{uuid.uuid4().hex[:8]}"
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        args = _parse_arguments(raw_args)
        logger.info(
            "tool start: round=%s name=%s call_id=%s args_keys=%s",
            round_idx,
            name,
            call_id,
            sorted(args.keys()),
        )

        # Plan Mode 服务端工具管控：在真正的 runner 执行前先过策略检查。
        # 这比 prompt 层面的"plan mode 请勿写入"更可靠——prompt 可能被模型忽略。
        # 策略拒绝时仍然 emit ToolStart + ToolComplete 事件，保证 UI 工具面板
        # 能正常展示"被拒绝"状态（is_error=True），而不是静默丢失工具调用记录。
        if execution_policy is not None:
            denied = self._execution_policy_denial(
                execution_policy=execution_policy,
                name=name,
                args=args,
                call_id=call_id,
                round_idx=round_idx,
            )
            if denied is not None:
                return denied

        denied = self._platform_permission_denial(
            name=name,
            args=args,
            call_id=call_id,
            round_idx=round_idx,
        )
        if denied is not None:
            return denied

        # PreToolUse hook：平台权限通过后、工具真正执行前的可配置拦截层。
        # 复用与平台权限同款的"拒绝即回灌结构化消息"模式；也可改写工具输入。
        if self._hook_manager is not None and self._hook_manager.has_event("PreToolUse"):
            outcome = self._hook_manager.fire(
                "PreToolUse",
                {
                    "tool_name": name,
                    "tool_input": args,
                    "tool_call_id": call_id,   # 传入 call_id，使 hook 能关联到具体的工具调用实例
                },
                matcher_value=name,
                round_idx=round_idx,
            )
            if outcome.blocked:
                result = self._cap_model_visible_result(
                    _hook_blocked_payload(name, args, outcome.block_reason),
                    call_id=call_id,
                    name=name,
                )
                logger.warning(
                    "tool blocked by PreToolUse hook: round=%s name=%s call_id=%s reason=%s",
                    round_idx, name, call_id, outcome.block_reason,
                )
                if self._bus is not None:
                    self._bus.emit(ToolComplete(
                        call_id=call_id, name=name, result=result,
                        duration_seconds=0.0, is_error=True,
                        round_idx=round_idx,
                    ))
                return ToolCallResult(
                    call_id=call_id, name=name, arguments=args,
                    result=result, duration_seconds=0.0, is_error=True,
                    status=ToolTerminalStatus.FAILED,
                    effect_state=ToolEffectState.NONE,
                )
            if outcome.updated_input is not None:
                logger.info(
                    "tool input rewritten by PreToolUse hook: round=%s name=%s call_id=%s",
                    round_idx, name, call_id,
                )
                args = outcome.updated_input
                # Hook 改写后得到的参数才是真正会交给工具的最终输入。角色策略和
                # 通讯平台权限必须再校验一次，不能让改写绕过工作区或用户权限边界。
                if execution_policy is not None:
                    denied = self._execution_policy_denial(
                        execution_policy=execution_policy,
                        name=name,
                        args=args,
                        call_id=call_id,
                        round_idx=round_idx,
                    )
                    if denied is not None:
                        return denied
                denied = self._platform_permission_denial(
                    name=name,
                    args=args,
                    call_id=call_id,
                    round_idx=round_idx,
                )
                if denied is not None:
                    return denied

        # Hook 可能改写 timeout 等参数，因此必须在所有策略检查完成后再解析
        # deadline 并创建子取消上下文。被策略拒绝的调用也不会残留父级回调。
        mode, tool_default = self._execution_profile(name)
        timeout_seconds = self._timeout_policy.resolve(
            tool_name=name,
            arguments=args,
            tool_default_seconds=tool_default,
        )
        deadline = self._timeout_policy.deadline_after(timeout_seconds)
        parent = cancel_token or CancelToken()
        child = parent.child(deadline=deadline)
        context = ToolExecutionContext(
            turn_id=turn_id,
            round_idx=round_idx,
            call_id=call_id,
            tool_name=name,
            cancellation=child,
            deadline=deadline,
        )

        if start_callback is not None:
            # 开始检查点是副作用工具的预写日志。写入失败时必须阻止工具启动，
            # 否则重启后会把已经执行过的调用误判为 cancelled_before_start。
            try:
                start_callback(context, args)
            except BaseException:
                child.close()
                raise
        if self._bus is not None:
            self._bus.emit(ToolStart(
                call_id=call_id, name=name, arguments=args, round_idx=round_idx,
            ))

        start = time.perf_counter()
        started_at = self._utc_now()
        status = ToolTerminalStatus.COMPLETED
        effect_state = ToolEffectState.COMPLETED
        cancel_reason: Optional[CancellationReason] = None
        runner_started = False
        try:
            child.throw_if_cancelled()
            runner_started = True
            if self._runner_accepts_context:
                result = self._runner(name, args, context)
            else:
                result = self._runner(name, args)
            completed_at = time.time()
            completed_monotonic = time.monotonic()
            if child.is_cancelled():
                reason = child.reason or CancellationReason.USER_CANCELLED
                # 以工具函数返回的瞬间作为完成边界。取消早于该边界才赢得竞争；
                # 迟到的用户取消不能覆盖已经完成的工具事实。
                timeout_won = (
                    reason == CancellationReason.TOOL_TIMEOUT
                    and deadline is not None
                    and deadline <= completed_monotonic
                )
                cancel_won = (
                    child.cancelled_at is not None
                    and child.cancelled_at <= completed_at
                )
                if timeout_won or cancel_won:
                    raise ToolCancelledError(
                        reason,
                        partial_output=_stringify_tool_result(result),
                        effect_state="may_have_occurred",
                    )
        except ToolCancelledError as exc:
            cancel_reason = exc.reason
            status = (
                ToolTerminalStatus.TIMED_OUT
                if exc.reason == CancellationReason.TOOL_TIMEOUT
                else ToolTerminalStatus.CANCELLED
            )
            if not runner_started:
                effect_state = ToolEffectState.NONE
            else:
                try:
                    effect_state = ToolEffectState(exc.effect_state)
                except ValueError:
                    effect_state = ToolEffectState.UNKNOWN
            result = self._terminal_payload(
                status=status,
                name=name,
                call_id=call_id,
                reason=exc.reason,
                effect_state=effect_state,
                partial_output=exc.partial_output,
            )
        except Exception as e:  # noqa: BLE001
            logger.exception("工具执行抛异常: name=%s call_id=%s", name, call_id)
            result = json.dumps(
                {"error": f"工具执行异常: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
            status = ToolTerminalStatus.FAILED
            effect_state = ToolEffectState.UNKNOWN
        finally:
            child.close()
        duration = time.perf_counter() - start
        if mode == ToolCancellationMode.BLOCKING and duration >= 0.1:
            logger.warning(
                "不可抢占工具执行较慢: name=%s call_id=%s duration=%.2fs",
                name,
                call_id,
                duration,
            )
        result = _stringify_tool_result(result)
        is_error = status != ToolTerminalStatus.COMPLETED

        # PostToolUse hook：工具成功执行后触发，可注入额外上下文给模型。
        # additional_context 追加进 result JSON 的约定字段 _hook_context，零协议改动：
        # 模型读到的 tool 消息里多一段 hook 注入的提示，AgentSession 无需特殊处理。
        if (
            not is_error
            and self._hook_manager is not None
            and self._hook_manager.has_event("PostToolUse")
        ):
            outcome = self._hook_manager.fire(
                "PostToolUse",
                {
                    "tool_name": name,        # 工具名称
                    "tool_input": args,        # 工具输入参数
                    "tool_response": result,   # 工具执行结果
                    "tool_call_id": call_id,   # 传入 call_id，使 hook 能关联到具体的工具调用实例
                },
                matcher_value=name,
                round_idx=round_idx,
            )
            if outcome.additional_context:
                result = _append_hook_context(result, outcome.additional_context)

        logger.info(
            "tool complete: round=%s name=%s call_id=%s is_error=%s duration=%.2fs result_chars=%s",
            round_idx,
            name,
            call_id,
            is_error,
            duration,
            len(result) if isinstance(result, str) else len(str(result)),
        )

        # 成功和异常结果都必须经过统一 10K token 上限。异常文本同样可能携带
        # 超长 stderr 或第三方响应，不能绕过模型上下文的最终安全边界。
        result = self._cap_model_visible_result(result, call_id=call_id, name=name)
        if _result_declares_error(result):
            is_error = True
            if status == ToolTerminalStatus.COMPLETED:
                status = ToolTerminalStatus.FAILED

        if self._bus is not None:
            self._bus.emit(ToolComplete(
                call_id=call_id, name=name, result=result,
                duration_seconds=duration, is_error=is_error,
                round_idx=round_idx,
            ))

        return ToolCallResult(
            call_id=call_id, name=name, arguments=args,
            result=result, duration_seconds=duration, is_error=is_error,
            status=status, effect_state=effect_state,
            cancel_reason=cancel_reason, started_at=started_at,
            finished_at=self._utc_now(),
        )

    def _execution_profile(self, name: str) -> tuple[ToolCancellationMode, Any]:
        if callable(self._profile_resolver):
            return self._profile_resolver(name)
        return ToolCancellationMode.BLOCKING, ...

    @staticmethod
    def _terminal_payload(
        *,
        status: ToolTerminalStatus,
        name: str,
        call_id: str,
        reason: CancellationReason | str,
        effect_state: ToolEffectState,
        partial_output: str = "",
    ) -> str:
        """生成模型可见的稳定终态结构。"""

        return json.dumps({
            "status": status.value,
            "tool": name,
            "call_id": call_id,
            "reason": reason.value if isinstance(reason, CancellationReason) else str(reason),
            "effect_state": effect_state.value,
            "partial_output": partial_output,
        }, ensure_ascii=False)

    def _cancelled_before_start(
        self,
        tool_call: Dict[str, Any],
        cancel_token: Optional[CancelToken],
        round_idx: int,
    ) -> ToolCallResult:
        """为未启动调用生成成对终态，避免产生孤立 tool_call。"""
        call_id = tool_call.get("id", "") or f"call_{uuid.uuid4().hex[:8]}"
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        args = _parse_arguments(raw_args)
        reason = (
            cancel_token.reason
            if cancel_token is not None and cancel_token.reason is not None
            else CancellationReason.USER_CANCELLED
        )
        result = ToolCallResult(
            call_id=call_id,
            name=name,
            arguments=args,
            result=self._terminal_payload(
                status=ToolTerminalStatus.CANCELLED_BEFORE_START,
                name=name, call_id=call_id, reason=reason,
                effect_state=ToolEffectState.NONE,
            ),
            duration_seconds=0.0,
            is_error=True,
            status=ToolTerminalStatus.CANCELLED_BEFORE_START,
            effect_state=ToolEffectState.NONE,
            cancel_reason=reason,
            finished_at=self._utc_now(),
        )
        if self._bus is not None:
            self._bus.emit(ToolComplete(
                call_id=call_id,
                name=name,
                result=result.result,
                duration_seconds=0.0,
                is_error=True,
                round_idx=round_idx,
            ))
        return result


__all__ = [
    "ToolExecutor",
    "ToolCallResult",
    "should_parallelize",
    "READ_ONLY_TOOLS",
    "READ_ONLY_IF_ACTION",
]
