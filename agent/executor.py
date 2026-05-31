"""工具执行调度器

把 LLM 一轮返回的 tool_calls 合理调度执行：
- **全部是只读**（file_read / search / bash_task list/output/wait）→ 线程池并发
- **任一非只读**（bash / file_write / todo / bash_permission / skill / MCP）→ 串行

为什么不更激进地"按工具组合"细判：
- bash 命令的"是否只读"是命令文本动态决定的（cat 只读 vs rm 写），但 cb-agent
  内部 bash_classify 判定逻辑分散，调度层不应反向耦合 bash 内部
- file_read + file_write 看似可"并发不同文件"，但 file_write 的 stale check
  依赖 ReadStateRegistry 共享态，同时跑的并发收益不抵复杂度
- 简单"全只读才并发"覆盖 80% 的真并发收益场景（一轮多个 grep / search /
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
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set

from agent.event_bus import EventBus
from agent.events import ToolComplete, ToolStart

logger = logging.getLogger(__name__)


# ========== 并发判定 ==========


# 纯读工具白名单：执行不写任何全局态、不调用副作用 API。
# 命中本集合的工具，组合并发是安全的。
READ_ONLY_TOOLS: Set[str] = {
    "file_read",
    "search",
    "rag",                 # 仅 query
    "memory_search",
    "skill",               # 加载 SKILL.md 是读盘
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


# ========== 调度器 ==========


class ToolExecutor:
    """工具调度器。AgentSession 一轮 tool_calls 来一批，调它跑完拿结果。"""

    def __init__(
        self,
        runner: ToolRunner,
        event_bus: Optional[EventBus] = None,
        max_workers: int = 4,
    ) -> None:
        """
        Args:
            runner: 接受 (tool_name, args_dict) 返回 str 的可调用。
                    通常是 ToolRegistry.execute_tool。
            event_bus: 可选事件总线，发 ToolStart / ToolComplete
            max_workers: 并发线程池大小上限。一批超过这个数仍并发，但被池内排队。
        """
        self._runner = runner
        self._bus = event_bus
        self._max_workers = max_workers

    def execute(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int = 0,
    ) -> List[ToolCallResult]:
        """执行一批 tool_calls，返回**保持 tool_calls 输入顺序**的结果列表。

        消息回灌的顺序必须跟模型 tool_calls 里一致（OpenAI 协议要求每个
        tool_call_id 对应一个 tool 消息），这里返回结果保序，方便 AgentSession
        直接 zip。
        """
        if not tool_calls:
            return []

        if should_parallelize(tool_calls):
            return self._execute_parallel(tool_calls, round_idx)
        return self._execute_serial(tool_calls, round_idx)

    # ---------- 串行 ----------

    def _execute_serial(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int,
    ) -> List[ToolCallResult]:
        return [self._run_one(tc, round_idx) for tc in tool_calls]

    # ---------- 并行 ----------

    def _execute_parallel(
        self,
        tool_calls: List[Dict[str, Any]],
        round_idx: int,
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
                    self._run_one, tc, round_idx,
                ): i
                for i, tc in enumerate(tool_calls)
            }
            for fut, idx in futs.items():
                results[idx] = fut.result()
        return [r for r in results if r is not None]

    # ---------- 单条 ----------

    def _run_one(
        self,
        tool_call: Dict[str, Any],
        round_idx: int,
    ) -> ToolCallResult:
        call_id = tool_call.get("id", "") or f"call_{uuid.uuid4().hex[:8]}"
        name = tool_call.get("function", {}).get("name", "")
        raw_args = tool_call.get("function", {}).get("arguments", "{}")
        args = _parse_arguments(raw_args)

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


__all__ = [
    "ToolExecutor",
    "ToolCallResult",
    "should_parallelize",
    "READ_ONLY_TOOLS",
    "READ_ONLY_IF_ACTION",
]
