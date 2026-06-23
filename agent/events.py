"""Agent 事件类型集合

cb-agent 的事件流系统：所有从 AgentSession / LLMStream / ToolExecutor 流出的
"消息"统一表达为 dataclass 事件，再经 EventBus 派发给订阅者（CLI / TUI / Web）。

设计原则：
- 事件是**只读快照**，订阅者不要回写
- 字段全部用基础类型 + dataclass，便于后续 JSON 序列化（Web/SSE 推送）
- 事件按"时间发生序"被 emit，但跨类型不保证强顺序
- LLM 层关心的事件 vs 工具层关心的事件 vs Agent 层关心的事件，全部在这一个文件里集中定义
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union


def _now() -> float:
    return time.time()


# ========== LLM 流式事件 ==========


@dataclass
class TextDelta:
    """模型流式 content 增量（assistant 正文）。"""
    delta: str
    accumulated: str = ""        # 当前轮累积全文（订阅者要 markdown 重渲染时用）
    round_idx: int = 0           # 工具循环的第几轮（1-based）
    timestamp: float = field(default_factory=_now)
    type: str = field(default="text_delta", init=False)


@dataclass
class ReasoningDelta:
    """模型流式 reasoning_content 增量（DeepSeek thinking 等）。"""
    delta: str
    accumulated: str = ""
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="reasoning_delta", init=False)


@dataclass
class TokenUsage:
    """流式响应结束时的 token 用量与 prompt cache 遥测。

    基础字段(所有 provider 保证返回):
        prompt_tokens:   输入 token 数(含 system + history + user)
        completion_tokens: 输出 token 数(含 reasoning_content)
        total_tokens:    prompt + completion

    Prompt cache 遥测字段(按 provider 能力可选,None = 不支持):
        cached_prompt_tokens:  被 provider 端缓存命中而免计算的 prompt token 数。
                               跨 provider 归一后的统一字段——OpenAI 路径来自
                               usage.prompt_tokens_details.cached_tokens,
                               Anthropic 路径等价于 prompt_cache_hit_tokens。
        prompt_cache_hit_tokens: 缓存命中的 token 数(Anthropic 原生字段;部分
                                 OpenAI 兼容厂商也直接返回顶层 hit/miss 拆分)。
        prompt_cache_miss_tokens: 缓存未命中,需重新计算的部分。
        cache_hit_rate:          缓存命中率(0.0~1.0),hit/(hit+miss) 或
                                 cached/prompt_tokens 的近似值。

    这些字段均为 Optional,下游渲染/日志应做好 None 检查。"""
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cached_prompt_tokens: Optional[int] = None
    prompt_cache_hit_tokens: Optional[int] = None
    prompt_cache_miss_tokens: Optional[int] = None
    cache_hit_rate: Optional[float] = None
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="token_usage", init=False)


# ========== 工具事件 ==========


@dataclass
class ToolCallPlanned:
    """LLM 流式累积出一个完整的 tool_call（finish_reason=tool_calls 时）。

    注意：这只是模型**计划**调用，工具还没真正执行。真正执行由 ToolExecutor 发 ToolStart。
    """
    call_id: str
    name: str
    arguments_json: str
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="tool_call_planned", init=False)


@dataclass
class ToolStart:
    """ToolExecutor 实际开始执行某个工具。"""
    call_id: str
    name: str
    arguments: Dict[str, Any]
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="tool_start", init=False)


@dataclass
class ToolComplete:
    """工具执行结束（成功或失败）。"""
    call_id: str
    name: str
    result: str                  # JSON 字符串（工具的 run() 返回值）
    duration_seconds: float
    is_error: bool = False
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="tool_complete", init=False)


# ========== 轮次事件 ==========


@dataclass
class RoundStart:
    """工具循环新一轮开始。"""
    round_idx: int
    max_rounds: int
    timestamp: float = field(default_factory=_now)
    type: str = field(default="round_start", init=False)


@dataclass
class RoundEnd:
    """一轮结束（要么模型给最终答案，要么进入下一轮）。"""
    round_idx: int
    has_tool_calls: bool         # 本轮是否有工具调用
    final: bool = False          # 是否本轮就是最终回答
    timestamp: float = field(default_factory=_now)
    type: str = field(default="round_end", init=False)


@dataclass
class ContextWindowUpdated:
    """工具循环中的上下文窗口估算刷新。

    Done 事件仍会携带最终的跨轮 state/history 估算；这个事件用于 UI 在工具
    循环尚未结束时刷新 Context 指标，通常 scope=current_request。
    """
    context_window: Dict[str, Any]
    reason: str = "tool_loop"
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="context_window_updated", init=False)


@dataclass
class Done:
    """整个 chat 调用结束（最终回答已就绪）。"""
    final_answer: str
    rounds_used: int
    cancelled: bool = False
    # 当前 active 会话的动态上下文窗口估算。它不是 API usage，而是“下一轮会话
    # state/history 大约占用多少上下文窗口”。TUI 用它刷新底部 Context 指标。
    context_window: Optional[Dict[str, Any]] = None
    # 自动上下文压缩审计信息。它只描述本轮是否为了保护上下文窗口做过 compact
    # 或 tool result 摘要替换；不包含完整工具输出，也不会被当成助手回答渲染。
    auto_compact: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=_now)
    type: str = field(default="done", init=False)


# ========== 错误与中断 ==========


@dataclass
class Error:
    """非致命错误（LLM 调用失败、工具执行异常等）。"""
    where: str                   # 'llm' / 'tool' / 'session'
    message: str
    exception_type: Optional[str] = None
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="error", init=False)


@dataclass
class Cancelled:
    """用户中断（CancelToken 触发）。"""
    where: str                   # 'llm_stream' / 'tool' / 'between_rounds'
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="cancelled", init=False)


# ========== 后台任务通知 ==========


@dataclass
class BackgroundNotification:
    """drain 出来的后台任务完成通知，每轮 think 前注入。"""
    task_id: str
    status: str                  # 'done' / 'failed' / 'killed'
    exit_code: Optional[int]
    output_path: str
    timestamp: float = field(default_factory=_now)
    type: str = field(default="background_notification", init=False)


# ========== 用户问答（AskUserQuestionTool） ==========


@dataclass
class AskUserQuestion:
    """工具向用户发起一个多选/单选问题，等待用户在 UI 端选择。

    协议约定：
    - 工具线程 emit 后阻塞等 QuestionRegistry.submit_answer(question_id, ...)
    - UI 收到事件 → 渲染 panel → 用户选择 → RPC session.answer_question
    - cancel_token 被触发或工具超时 → 工具方返回 cancelled 结果
    """
    question_id: str
    question: str
    options: List[Dict[str, str]]   # [{label, description}, ...]
    multi_select: bool = False
    recommended_index: Optional[int] = None
    allow_other: bool = True        # 允许 "Other" 自定义文本
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="ask_user_question", init=False)


@dataclass
class AskUserQuestionAnswered:
    """用户已对某条问题作答；UI 收到此事件可关闭 panel。"""
    question_id: str
    selected_labels: List[str]      # 多选给多个；单选给一个
    other_text: Optional[str] = None  # 用户选 "Other" 时填的自定义文本
    cancelled: bool = False         # True=用户取消（不作答）
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="ask_user_question_answered", init=False)


# ========== Todo 事件 ==========


@dataclass
class TodoListUpdated:
    """todo 工具写入后的结构化广播；UI 用它单独渲染 todo 面板，
    避免靠解析 tool_complete 的 JSON 字符串。

    items: list of {id: str, content: str, status: str}
        status ∈ {pending, in_progress, completed, cancelled}
    每次都是**全量列表**（不是 diff），UI 直接整片替换/追加一张新卡片。
    """
    items: List[Dict[str, str]]
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="todo_list_updated", init=False)


@dataclass
class MCPStatus:
    """MCP 后台加载状态快照。

    MCP 服务器可能需要启动外部进程、下载 npm 包或连接远端服务，不能再阻塞 agent
    启动关键路径。这个事件只用于 UI/CLI 展示“正在连接/已连接/失败”，不进入
    会话 history，也不参与下一轮 prompt，避免把运行时状态污染成长期上下文。
    """
    status: str                         # disabled / loading / ready / error
    servers: List[Dict[str, Any]]       # [{name, status, tools_count, error?, elapsed_seconds?}]
    total: int = 0
    connected: int = 0
    failed: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="mcp_status", init=False)


@dataclass
class PetUpdated:
    """Desktop pet state update.

    Pet state is UI/runtime state. It does not enter conversation history and it
    does not participate in tool traces. Frontends replace the whole snapshot.
    """
    state: Dict[str, Any]
    reason: str = "update"
    timestamp: float = field(default_factory=_now)
    type: str = field(default="pet_updated", init=False)


# ========== Plan Mode 事件 ==========
# Plan Mode 是协作式双模式（plan / execute）的事件体系。
# 流程：用户切到 plan 模式 → LLM 输出 <proposed_plan> 块
# → 流式解析出 PlanStart / PlanDelta 事件 → 解析完毕后 emit PlanReady
# → 用户审批（approve/reject）→ PlanApproved / PlanRejected → PlanModeChanged。


@dataclass
class PlanModeChanged:
    """当前会话的协作模式发生变化（plan ↔ execute）。

    当用户通过 /plan 命令或 UI 切换协作模式时触发。
    前端应据此切换工具栏、输入框提示和工具列表展示。
    """
    mode: str
    plan_state: Dict[str, Any]
    timestamp: float = field(default_factory=_now)
    type: str = field(default="plan_mode_changed", init=False)


@dataclass
class PlanStart:
    """流式解析器在 LLM 输出中检测到 <proposed_plan> 开始标签。

    此时前端应重置计划面板，准备接收后续 PlanDelta 增量。
    """
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="plan_start", init=False)


@dataclass
class PlanDelta:
    """<proposed_plan> 块内的流式 Markdown 增量。

    与 TextDelta 类似，但 delta 只包含计划块内的文本（不含块外正常回答）。
    accumulated 是当前计划块从 PlanStart 到现在的累计文本。
    """
    delta: str
    accumulated: str = ""
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="plan_delta", init=False)


@dataclass
class PlanReady:
    """流式解析器检测到 </proposed_plan> 结束标签，计划块完整就绪。

    此时 pending plan 已持久化到磁盘（plan/current.md），
    前端应展示审批按钮（approve / reject）。
    """
    plan: str
    plan_state: Dict[str, Any]
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="plan_ready", init=False)


@dataclass
class PlanApproved:
    """用户批准了 pending plan，后端已切回 execute 模式。

    approved_plan 内容进入上下文注入，LLM 将在 execute 模式下按计划实施。
    同时 emit PlanModeChanged(mode="execute")。
    """
    plan: str
    plan_state: Dict[str, Any]
    timestamp: float = field(default_factory=_now)
    type: str = field(default="plan_approved", init=False)


@dataclass
class PlanRejected:
    """用户拒绝了 pending plan 并提供了修改反馈。

    反馈文本（feedback）会注入下一轮 plan 模式的上下文，
    LLM 应读取反馈并提交修订后的替代计划。
    同时 emit PlanModeChanged(mode="plan")，保持在 plan 模式。
    """
    feedback: str
    plan_state: Dict[str, Any]
    timestamp: float = field(default_factory=_now)
    type: str = field(default="plan_rejected", init=False)


# ========== Hook 事件（HookManager 触发 hook 时的可见性广播）==========


@dataclass
class SubagentStarted:
    """子代理（child agent）开始运行的事件。

    当主代理（root agent）启动一个子代理任务时广播此事件，
    用于 UI 展示"子代理启动"的状态变化。
    """
    subagent_id: str           # 子代理的唯一标识
    subagent_type: str         # 子代理的类型（如 "agent_tool" / "agent_task"）
    description: str           # 子代理的任务描述
    task_id: Optional[str] = None       # 后台任务 ID（运行于后台时才有）
    run_in_background: bool = False     # 是否以后台任务方式运行
    parent_session_id: Optional[str] = None  # 父会话的 session_id
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="subagent_started", init=False)


@dataclass
class SubagentProgress:
    """子代理运行过程中的进度更新事件。

    子代理在执行中周期性地 emit 紧凑进度消息，
    UI 端可以根据 status 展示"运行中"状态。
    """
    subagent_id: str           # 子代理的唯一标识
    subagent_type: str         # 子代理的类型
    message: str               # 进度描述文本
    task_id: Optional[str] = None       # 绑定的后台任务 ID
    status: str = "running"             # 当前状态（running / ...）
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="subagent_progress", init=False)


@dataclass
class SubagentCompleted:
    """子代理运行结束的事件（成功、失败或取消）。

    包含最终的输出内容、执行时长、使用轮数等汇总信息，
    UI 端根据 is_error 切换成功/失败的颜色渲染。
    """
    subagent_id: str           # 子代理的唯一标识
    subagent_type: str         # 子代理的类型
    description: str           # 任务描述（与 started 事件一致，方便 UI 聚合）
    status: str                # 结束状态：completed / failed / cancelled
    content: str = ""          # 子代理的最终回答文本
    task_id: Optional[str] = None       # 绑定的后台任务 ID
    output_path: Optional[str] = None   # 输出文件路径（如有持久化）
    duration_seconds: float = 0.0       # 子代理执行总耗时
    rounds_used: int = 0                # 子代理使用的工具循环轮数
    is_error: bool = False              # 是否以错误结束
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="subagent_completed", init=False)


@dataclass
class HookStarted:
    """某个 hook handler 开始执行。

    HookManager 的控制流是双向的（要收集决策影响主流程），但仍通过 EventBus
    emit 这个只读事件，让前端能看到"某个 hook 正在跑"。
    """
    event_name: str              # "PreToolUse" / "PostToolUse" / ...
    handler_type: str            # 目前只有 "command"
    matcher: str                 # 命中的 matcher 字段值
    round_idx: int = 0
    hook_call_id: str = ""                    # 本次 hook 调用的唯一标识，用于关联 started/completed 配对
    agent_scope: str = "root"                  # 代理作用域："root" 主代理 / "subagent" 子代理
    subagent_id: Optional[str] = None          # 子代理 ID（仅子代理作用域时有值）
    subagent_type: Optional[str] = None        # 子代理类型
    parent_session_id: Optional[str] = None    # 父会话 ID
    task_id: Optional[str] = None             # 后台任务 ID
    run_in_background: bool = False           # 是否运行在后台
    timestamp: float = field(default_factory=_now)
    type: str = field(default="hook_started", init=False)


@dataclass
class HookCompleted:
    """某个 hook handler 执行完毕。"""
    event_name: str
    blocked: bool                # 是否阻止了主操作
    has_context: bool            # 是否注入了 additional_context
    duration_seconds: float
    hook_call_id: str = ""                    # 本次 hook 调用的唯一标识，与 HookStarted 配对
    agent_scope: str = "root"                  # 代理作用域："root" 主代理 / "subagent" 子代理
    subagent_id: Optional[str] = None          # 子代理 ID（仅子代理作用域时有值）
    subagent_type: Optional[str] = None        # 子代理类型
    parent_session_id: Optional[str] = None    # 父会话 ID
    task_id: Optional[str] = None             # 后台任务 ID
    run_in_background: bool = False           # 是否运行在后台
    round_idx: int = 0
    timestamp: float = field(default_factory=_now)
    type: str = field(default="hook_completed", init=False)


# ========== Union 类型（订阅者用 isinstance 区分）==========


Event = Union[
    TextDelta,
    ReasoningDelta,
    TokenUsage,
    ToolCallPlanned,
    ToolStart,
    ToolComplete,
    RoundStart,
    RoundEnd,
    ContextWindowUpdated,
    Done,
    Error,
    Cancelled,
    BackgroundNotification,
    AskUserQuestion,
    AskUserQuestionAnswered,
    TodoListUpdated,
    MCPStatus,
    PetUpdated,
    PlanModeChanged,
    PlanStart,
    PlanDelta,
    PlanReady,
    PlanApproved,
    PlanRejected,
    SubagentStarted,
    SubagentProgress,
    SubagentCompleted,
    HookStarted,
    HookCompleted,
]


__all__ = [
    "Event",
    "TextDelta",
    "ReasoningDelta",
    "TokenUsage",
    "ToolCallPlanned",
    "ToolStart",
    "ToolComplete",
    "RoundStart",
    "RoundEnd",
    "ContextWindowUpdated",
    "Done",
    "Error",
    "Cancelled",
    "BackgroundNotification",
    "AskUserQuestion",
    "AskUserQuestionAnswered",
    "TodoListUpdated",
    "MCPStatus",
    "PetUpdated",
    "PlanModeChanged",
    "PlanStart",
    "PlanDelta",
    "PlanReady",
    "PlanApproved",
    "PlanRejected",
    "SubagentStarted",
    "SubagentProgress",
    "SubagentCompleted",
    "HookStarted",
    "HookCompleted",
]
