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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from agent.cancel import CancelToken
from agent.event_bus import EventBus
from agent.events import Cancelled, ToolComplete, ToolStart
from agent.platforms.permissions import (
    check_platform_tool_permission,
    permission_denied_payload,
)
from agent.result_cap import cap_batch_results, cap_single_result

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


# Tool registry 接口最小描述：只用 execute_tool(name, args) -> str
ToolRunner = Callable[[str, Dict[str, Any]], str]


@dataclass
class ToolCallResult:
    """单条工具执行结果。"""
    call_id: str
    name: str
    arguments: Dict[str, Any]
    result: str               # 工具的 run() 返回（通常 JSON 字符串）
    duration_seconds: float
    is_error: bool


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

    def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int = 0,
        cancel_token: Optional[CancelToken] = None,
        execution_policy: Optional[Any] = None,
        result_callback: Optional[Callable[[ToolCallResult], None]] = None,
    ) -> List[ToolCallResult]:
        """执行一批 tool_calls，返回**保持 tool_calls 输入顺序**的结果列表。

        消息回灌的顺序必须跟模型 tool_calls 里一致（OpenAI 协议要求每个
        tool_call_id 对应一个 tool 消息），这里返回结果保序，方便 AgentSession
        直接 zip。

        cancel_token 行为：
          - 串行：在每个工具开始前看一眼。已被 cancel 则剩余 tool_calls 全部
            填一个"已取消"的占位结果（保留 call_id 让回灌不破协议）
          - 并行：所有 future 都正常 submit / 等回；submit 之前最后一次看
            token——已 cancel 就一个都不发，全部填占位。已 submit 的工具不
            被强制中止（线程池不可中断；这跟 LLM 流式不一样，工具进程要靠它
            自己的超时机制处理硬中断）

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
            if self._bus is not None:
                self._bus.emit(Cancelled(where="executor", round_idx=round_idx))
            return [
                self._cancelled_placeholder(tc) for tc in tool_calls
            ]

        if should_parallelize(tool_calls):
            logger.info("executor mode: parallel round=%s calls=%s", round_idx, len(tool_calls))
            results = self._execute_parallel(
                tool_calls,
                round_idx,
                cancel_token,
                execution_policy,
                result_callback,
            )
        else:
            logger.info("executor mode: serial round=%s calls=%s", round_idx, len(tool_calls))
            results = self._execute_serial(
                tool_calls,
                round_idx,
                cancel_token,
                execution_policy,
                result_callback,
            )

        # 批量总量上限检查：单轮所有 tool results 总字符超限时从最长的开始持久化
        # result_callback 已经在单个工具完成时通知过一次；这里如果批量 cap 又改写
        # 了结果，需要用同一个 call_id 再通知一次，让 active_turn 中的最终结果与
        # 后续回灌给模型的 messages 保持一致。
        before_batch_cap = [r.result for r in results]
        cap_batch_results(results, self._persist_dir)
        for before, result in zip(before_batch_cap, results):
            if result.result != before:
                self._notify_result_callback(result_callback, result)
        return results

    # ---------- 串行 ----------

    def _execute_serial(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int,
        cancel_token: Optional[CancelToken],
        execution_policy: Optional[Any],
        result_callback: Optional[Callable[[ToolCallResult], None]],
    ) -> List[ToolCallResult]:
        """串行执行一批 tool_calls。

        每执行一个工具前检查 cancel_token；已取消的 tool_calls 填入占位结果，
        保证 OpenAI 协议每个 tool_call_id 都有对应的 tool 消息回灌。
        """
        results: List[ToolCallResult] = []
        cancel_emitted = False
        for tc in tool_calls:
            if cancel_token is not None and cancel_token.is_cancelled():
                if self._bus is not None and not cancel_emitted:
                    self._bus.emit(Cancelled(where="executor", round_idx=round_idx))
                    cancel_emitted = True
                logger.info(
                    "executor skipped tool after cancel: round=%s name=%s",
                    round_idx,
                    tc.get("function", {}).get("name", ""),
                )
                results.append(self._cancelled_placeholder(tc))
                continue
            result = self._run_one(tc, round_idx, execution_policy=execution_policy)
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
    ) -> List[ToolCallResult]:
        # 每个 worker 拿一份 ctx 副本：同一个 contextvars.Context 不能并发
        # 多次 ctx.run（会抛 "context already entered"）。我们在主线程抓
        # 状态，给每个 worker 各 copy_context() 一份独立 ctx。
        max_workers = min(len(tool_calls), self._max_workers)
        results: List[Optional[ToolCallResult]] = [None] * len(tool_calls)
        with ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cb-tool",
        ) as ex:
            futs = {
                ex.submit(
                    contextvars.copy_context().run,
                    self._run_one, tc, round_idx, execution_policy,
                ): i
                for i, tc in enumerate(tool_calls)
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                result = fut.result()
                results[idx] = result
                # 并行模式下按完成顺序通知持久化层，但返回值仍按 tool_calls
                # 原始顺序组装，保证后续 OpenAI tool 消息回灌顺序不变。
                self._notify_result_callback(result_callback, result)
        # 这里不主动看 cancel_token：所有 future 已经 submit，让它们自然
        # 结束更安全；token 是否被 set 由 cb_agents / session 在外层处理
        return [r for r in results if r is not None]

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

    def _run_one(
        self,
        tool_call: Dict[str, Any],
        round_idx: int,
        execution_policy: Optional[Any] = None,
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
            try:
                allowed, reason = execution_policy.check(name, args)
            except Exception as e:  # noqa: BLE001
                # 策略检查自身异常 → 保守拒绝，避免策略 bug 导致越权执行
                logger.exception("execution policy failed: name=%s call_id=%s", name, call_id)
                allowed, reason = False, f"execution policy error: {type(e).__name__}: {e}"
            if not allowed:
                result = execution_policy.denied_result(name, args, reason or "tool denied")
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
                )

        permission = check_platform_tool_permission(name, args)
        if permission.denied:
            result = permission_denied_payload(name, args, permission)
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
            )

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
                result = _hook_blocked_payload(name, args, outcome.block_reason)
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
                )
            if outcome.updated_input is not None:
                logger.info(
                    "tool input rewritten by PreToolUse hook: round=%s name=%s call_id=%s",
                    round_idx, name, call_id,
                )
                args = outcome.updated_input

        if self._bus is not None:
            self._bus.emit(ToolStart(
                call_id=call_id, name=name, arguments=args, round_idx=round_idx,
            ))

        start = time.perf_counter()
        is_error = False
        try:
            result = self._runner(name, args)
        except Exception as e:  # noqa: BLE001
            logger.exception("工具执行抛异常: name=%s call_id=%s", name, call_id)
            result = json.dumps(
                {"error": f"工具执行异常: {type(e).__name__}: {e}"},
                ensure_ascii=False,
            )
            is_error = True
        duration = time.perf_counter() - start
        result = _stringify_tool_result(result)

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

        # 统一结果上限：超过 MAX_SINGLE_RESULT_CHARS 时持久化到磁盘
        if not is_error:
            result, persisted = cap_single_result(
                result, call_id, name, self._persist_dir,
            )
            if persisted:
                logger.info(
                    "tool result persisted: name=%s call_id=%s dir=%s",
                    name, call_id, self._persist_dir,
                )

        if self._bus is not None:
            self._bus.emit(ToolComplete(
                call_id=call_id, name=name, result=result,
                duration_seconds=duration, is_error=is_error,
                round_idx=round_idx,
            ))

        return ToolCallResult(
            call_id=call_id, name=name, arguments=args,
            result=result, duration_seconds=duration, is_error=is_error,
        )

    # ---------- cancel 占位 ----------

    def _cancelled_placeholder(self, tool_call: Dict[str, Any]) -> ToolCallResult:
        """生成一个"被取消"的占位 ToolCallResult。

        OpenAI 协议要求每个 tool_call_id 必须有对应的 tool 消息回灌，否则
        下一轮 think 直接 400。这里用 is_error=True + 简短 JSON 既保留
        协议合法性，也告诉模型"这个工具因用户取消没跑"。
        """
        call_id = tool_call.get("id", "") or f"call_{uuid.uuid4().hex[:8]}"
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        args = _parse_arguments(raw_args)
        return ToolCallResult(
            call_id=call_id,
            name=name,
            arguments=args,
            result=json.dumps(
                {"cancelled": True, "reason": "user requested cancel"},
                ensure_ascii=False,
            ),
            duration_seconds=0.0,
            is_error=True,
        )


__all__ = [
    "ToolExecutor",
    "ToolCallResult",
    "should_parallelize",
    "READ_ONLY_TOOLS",
    "READ_ONLY_IF_ACTION",
]
