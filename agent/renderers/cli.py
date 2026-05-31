"""CLIRenderer：订阅 EventBus 事件流，把 agent 运行过程渲染到终端。

跟原 AgentRunner 的 print 行为保持一致：
- RoundStart      → '[round N] 调用模型 ...'
- TextDelta       → 'assistant > xxx'（流式追加；本批第一次出现时打前缀）
- ReasoningDelta  → 累积，等到 RoundEnd 才一次性渲染 'Thought for Xs' 块
- ToolCallPlanned → 不渲染（避免和 ToolStart 重复；planned 是模型决策，
                    Start 才是真正执行那一刻）
- ToolStart       → '→ 调用工具 name(args) [并发]'
- ToolComplete    → todo/bash 走彩色面板；其它走截断预览
- TokenUsage      → 暂不渲染（信息量太大，CLI 默认不打；前端可订阅自己加）
- BackgroundNotification → 蓝字提示
- Error           → 红字 [!] xxx
- Cancelled       → 黄字 ✗ 取消
- Done            → 不需要渲染（最终 answer 通过 TextDelta 流式打过了）

线程安全：CLI 当前是单线程消费 EventBus，但事件来自不同线程（LLM 流式在主线程，
工具并发在 worker 线程）。EventBus.emit 已经在订阅者侧拿快照，但 print 本身
**不是原子操作**——并发 ToolStart/ToolComplete 可能跟 LLM 流式 TextDelta 交错。
本 renderer 用 self._lock 包所有 print，避免一行被切两半。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional

from agent.event_bus import EventBus
from agent.events import (
    BackgroundNotification, Cancelled, Done, Error, ReasoningDelta,
    RoundEnd, RoundStart, TextDelta, TokenUsage, ToolComplete, ToolStart,
)

logger = logging.getLogger(__name__)


# ========== ANSI ==========


class _Ansi:
    """ANSI 颜色生成器。终端不支持 VT 时返回空串。"""

    def __init__(self) -> None:
        self.enabled = True
        if sys.platform == "win32":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                try:
                    import colorama  # 可选
                    colorama.just_fix_windows_console()
                except Exception:
                    self.enabled = False

    def code(self, c: str) -> str:
        return f"\033[{c}m" if self.enabled else ""


_A = _Ansi()
BOLD = _A.code("1")
DIM = _A.code("2")
RESET = _A.code("0")
RED = _A.code("31")
GREEN = _A.code("32")
YELLOW = _A.code("33")
BLUE = _A.code("34")
MAGENTA = _A.code("35")
CYAN = _A.code("36")
GRAY = _A.code("90")


# ========== 渲染辅助 ==========


def _short_args(args: Dict[str, Any], limit: int = 80) -> str:
    """把工具参数压成一行短预览。"""
    try:
        s = json.dumps(args, ensure_ascii=False)
    except Exception:
        s = str(args)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def _render_thought(reason: str, elapsed_seconds: Optional[float] = None) -> str:
    """模型 reasoning_content → 'Thought for Xs' 折叠风格。"""
    head_time = ""
    if elapsed_seconds is not None and elapsed_seconds >= 0:
        head_time = f" for {elapsed_seconds:.1f}s"
    head = f"{DIM}{CYAN}▸ Thought{head_time}{RESET}"
    body_lines = (reason or "").strip().splitlines()
    if not body_lines:
        return head
    indented = "\n".join(f"  {GRAY}{ln}{RESET}" for ln in body_lines)
    return f"{head}\n{indented}"


def _render_todo_panel(tool_result: str) -> Optional[str]:
    """把 todo 工具的 JSON 输出渲染成彩色面板。结构不符返回 None。"""
    try:
        data = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "todos" not in data:
        return None

    todos = data.get("todos") or []
    summary = data.get("summary") or {}

    style_map = {
        "completed":   ("☒", GREEN,  DIM),
        "in_progress": ("◐", YELLOW, BOLD),
        "pending":     ("☐", GRAY,   ""),
        "cancelled":   ("✗", GRAY,   DIM),
    }

    lines = [f"{BOLD}{MAGENTA}● Update Todos{RESET}"]
    if not todos:
        lines.append(f"  {GRAY}（当前没有任务）{RESET}")
    else:
        for item in todos:
            status = (item.get("status") or "pending").lower()
            marker, color, body_style = style_map.get(status, ("·", GRAY, ""))
            content = item.get("content") or "(无描述)"
            lines.append(f"  {color}{marker}{RESET} {body_style}{content}{RESET}")

    if summary:
        total = summary.get("total", len(todos))
        bits = []
        if summary.get("in_progress"): bits.append(f"{YELLOW}进行中 {summary['in_progress']}{RESET}")
        if summary.get("pending"):     bits.append(f"{GRAY}待办 {summary['pending']}{RESET}")
        if summary.get("completed"):   bits.append(f"{GREEN}完成 {summary['completed']}{RESET}")
        if summary.get("cancelled"):   bits.append(f"{GRAY}取消 {summary['cancelled']}{RESET}")
        tail = "  ".join(bits)
        lines.append(f"  {DIM}── 共 {total} 项{RESET}" + (f"  {tail}" if tail else ""))

    return "\n".join(lines)


def _render_bash_output(tool_result: str) -> Optional[str]:
    """把 bash 工具的 JSON 输出渲染成彩色面板。结构不符返回 None。"""
    try:
        data = json.loads(tool_result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict) or "stdout" not in data:
        return None

    stdout = (data.get("stdout") or "").rstrip()
    stderr = (data.get("stderr") or "").rstrip()
    exit_code = data.get("exit_code", 0)
    is_error = data.get("is_error", exit_code != 0)
    classification = data.get("classification", {}) or {}
    kind = classification.get("kind", "normal")
    semantic = data.get("semantic")
    interrupted = data.get("interrupted", False)
    timed_out = data.get("timeout", False)
    background = data.get("background", False)

    lines: List[str] = []

    if background:
        task_id = data.get("background_task_id", "?")
        lines.append(f"{DIM}{CYAN}  ⟳ 后台运行中 (task {task_id}){RESET}")
        return "\n".join(lines)

    if timed_out:
        lines.append(f"{BOLD}{YELLOW}  ⏱ 命令超时{RESET}")
    if interrupted:
        lines.append(f"{YELLOW}  ✗ 命令被中断{RESET}")

    if kind == "silent" and not is_error and not timed_out and not interrupted:
        lines.append(f"  {DIM}Done.{RESET}")
        return "\n".join(lines) if lines else None

    if is_error and not timed_out and not interrupted:
        stderr_preview = (stderr or f"exit code {exit_code}")[:200]
        lines.append(f"  {BOLD}{RED}✗ {stderr_preview}{RESET}")

    if not is_error and semantic:
        lines.append(f"  {DIM}({semantic.get('message', '')}){RESET}")

    if stdout:
        preview = stdout[:500]
        trimmed = "..." if len(stdout) > 500 else ""
        for line in preview.split("\n"):
            lines.append(f"  {DIM}{line}{RESET}")
        if trimmed:
            lines.append(f"  {GRAY}... [{len(stdout)} 字符, 已截断显示]{RESET}")

    if stderr and not is_error:
        for line in stderr.split("\n")[:5]:
            lines.append(f"  {YELLOW}{line}{RESET}")

    if not lines:
        return None

    if kind in ("search", "read", "list"):
        label = {"search": "搜索结果", "read": "读取内容", "list": "目录列表"}.get(kind, "输出")
        header = f"{BOLD}{MAGENTA}  ▸ {label}{RESET}"
        lines.insert(0, header)

    return "\n".join(lines)


# ========== Renderer ==========


class CLIRenderer:
    """订阅 EventBus 各事件，渲染到 stdout。

    用法：
        renderer = CLIRenderer(event_bus)
        renderer.attach()        # 注册订阅
        # ... session.chat(...) 期间事件会被自动渲染
        # renderer.detach()      # 可选；通常进程结束自动回收

    线程安全：所有 print 在 self._lock 内执行，避免并发事件交错切碎一行。
    """

    def __init__(
        self,
        event_bus: EventBus,
        show_token_usage: bool = False,
        show_planned: bool = False,
    ) -> None:
        self.bus = event_bus
        self.show_token_usage = show_token_usage
        self.show_planned = show_planned

        self._lock = threading.Lock()
        # 流式正文：是否已打印 'assistant > ' 前缀（每轮 RoundStart 重置）
        self._text_prefix_printed = False
        # 累积 reasoning_content，等本轮 RoundEnd 时一次性渲染 'Thought' 块
        self._reasoning_buf: List[str] = []
        self._round_start_time: float = 0.0
        # 并发批次的 ToolStart 比较密集；这里不去缓存判断"是否并发"，
        # 而是直接看 ToolStart 事件的 thread_id 区分（同一线程是串行）
        self._tool_start_thread_ids: List[int] = []

        # detach 时要 unsubscribe 的 (callback, event_type) 列表
        self._subs: List[tuple[Any, Any]] = []

    # ---------- 生命周期 ----------

    def attach(self) -> None:
        """订阅所有相关事件类型。可重复调用——会先 detach。"""
        self.detach()
        bindings = [
            (self._on_round_start, RoundStart),
            (self._on_round_end, RoundEnd),
            (self._on_text, TextDelta),
            (self._on_reasoning, ReasoningDelta),
            (self._on_tool_start, ToolStart),
            (self._on_tool_complete, ToolComplete),
            (self._on_token_usage, TokenUsage),
            (self._on_background, BackgroundNotification),
            (self._on_error, Error),
            (self._on_cancelled, Cancelled),
        ]
        for cb, evt in bindings:
            self.bus.subscribe(cb, evt)
            self._subs.append((cb, evt))
        # Done 留给上层 REPL 自己 collect

    def detach(self) -> None:
        for cb, evt in self._subs:
            try:
                self.bus.unsubscribe(cb, evt)
            except Exception:
                pass
        self._subs.clear()

    # ---------- 事件处理 ----------

    def _on_round_start(self, e: RoundStart) -> None:
        with self._lock:
            self._text_prefix_printed = False
            self._reasoning_buf = []
            self._round_start_time = time.perf_counter()
            self._tool_start_thread_ids = []
            print(f"\n[round {e.round_idx}] 调用模型 ...")

    def _on_round_end(self, e: RoundEnd) -> None:
        with self._lock:
            # 流式正文末尾补换行（如果本轮打了 assistant > 前缀）
            if self._text_prefix_printed:
                print()
                self._text_prefix_printed = False

            # reasoning 累积完整 → 渲染 Thought 块
            if self._reasoning_buf:
                reason = "".join(self._reasoning_buf)
                elapsed = time.perf_counter() - self._round_start_time
                print(_render_thought(reason, elapsed_seconds=elapsed))
                self._reasoning_buf = []

    def _on_text(self, e: TextDelta) -> None:
        with self._lock:
            if not self._text_prefix_printed:
                print(f"\n{BOLD}assistant >{RESET} ", end="", flush=True)
                self._text_prefix_printed = True
            print(e.delta, end="", flush=True)

    def _on_reasoning(self, e: ReasoningDelta) -> None:
        # 不实时渲染，只累积；RoundEnd 时统一打 Thought 块
        with self._lock:
            self._reasoning_buf.append(e.delta)

    def _on_tool_start(self, e: ToolStart) -> None:
        with self._lock:
            tid = threading.get_ident()
            self._tool_start_thread_ids.append(tid)
            # 同一 round 内多个 ToolStart 来自不同 thread → 并发标签
            distinct_threads = len(set(self._tool_start_thread_ids))
            tag = f" {DIM}{CYAN}[并发]{RESET}" if distinct_threads > 1 else ""
            args_short = _short_args(e.arguments)
            print(f"  {GREEN}→{RESET} 调用工具 {BOLD}{e.name}{RESET}({args_short}){tag}")

    def _on_tool_complete(self, e: ToolComplete) -> None:
        with self._lock:
            if e.is_error:
                preview = (e.result or "")[:200].replace("\n", " ")
                print(f"     {RED}← 错误: {preview}{RESET}")
                return

            if e.name == "todo":
                rendered = _render_todo_panel(e.result)
            elif e.name == "bash":
                rendered = _render_bash_output(e.result)
            else:
                rendered = None

            if rendered:
                print(rendered)
            else:
                preview = (e.result or "")[:200].replace("\n", " ")
                suffix = "..." if e.result and len(e.result) > 200 else ""
                duration_tag = f" {DIM}({e.duration_seconds:.2f}s){RESET}" if e.duration_seconds >= 0.5 else ""
                print(f"     {DIM}← 结果:{RESET} {preview}{suffix}{duration_tag}")

    def _on_token_usage(self, e: TokenUsage) -> None:
        if not self.show_token_usage:
            return
        with self._lock:
            print(
                f"  {DIM}{GRAY}[round {e.round_idx}] tokens: "
                f"prompt={e.prompt_tokens} completion={e.completion_tokens} "
                f"total={e.total_tokens}{RESET}"
            )

    def _on_background(self, e: BackgroundNotification) -> None:
        with self._lock:
            print(
                f"{BLUE}{BOLD}⟳ 后台任务完成{RESET} "
                f"task={e.task_id} status={e.status} exit={e.exit_code} "
                f"{DIM}→ {e.output_path}{RESET}"
            )

    def _on_error(self, e: Error) -> None:
        with self._lock:
            tag = f"[{e.where}]" if e.where else ""
            print(f"{RED}{BOLD}[!]{RESET} {tag} {e.message}", file=sys.stderr)

    def _on_cancelled(self, e: Cancelled) -> None:
        with self._lock:
            print(f"\n{YELLOW}{BOLD}✗ 已取消{RESET} {DIM}({e.where}){RESET}")


__all__ = [
    "CLIRenderer",
    # 渲染辅助函数也导出，方便单测和复用
    "_render_thought",
    "_render_todo_panel",
    "_render_bash_output",
    "_short_args",
]
