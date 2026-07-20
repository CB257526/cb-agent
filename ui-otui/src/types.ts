/**
 * cb-agent 事件类型 mirror。
 *
 * 字段跟 Python 端 agent/events.py 的 dataclass 序列化结果一致：
 *   asdict(TextDelta(...)) → {type: "text_delta", delta: "...", accumulated: "...", round_idx: 1, timestamp: 1.7e9}
 *
 * 协议层（JSON-RPC envelope）见 transport.ts。这里只描述 params 内的事件 payload。
 *
 * 不强求严格类型一一对应——agent 那边事件 schema 演进时这边松一点更好维护。
 */

export type EventType =
  | "text_delta"
  | "reasoning_delta"
  | "token_usage"
  | "tool_call_planned"
  | "tool_start"
  | "tool_complete"
  | "round_start"
  | "round_end"
  | "context_window_updated"
  | "done"
  | "error"
  | "cancelled"
  | "background_notification"
  | "ask_user_question"
  | "ask_user_question_answered"
  | "todo_list_updated"
  | "mcp_status"
  | "permission_mode_changed"
  | "model_changed"
  | "subagent_started"
  | "subagent_progress"
  | "subagent_completed"
  | "hook_started"
  | "hook_completed"
  | "plan_mode_changed"
  | "plan_start"
  | "plan_delta"
  | "plan_ready"
  | "plan_approved"
  | "plan_rejected"
  | "gateway_ready";  // gateway 自定义，不在 events.py 里

export interface BaseEvent {
  type: EventType;
  round_idx?: number;
  timestamp?: number;
}

export interface TextDelta extends BaseEvent {
  type: "text_delta";
  delta: string;
  accumulated: string;
}

export interface ReasoningDelta extends BaseEvent {
  type: "reasoning_delta";
  delta: string;
  accumulated: string;
}

export interface TokenUsage extends BaseEvent {
  type: "token_usage";
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  // prompt cache 遥测(仅支持该能力的 provider 返回)
  cached_prompt_tokens?: number;   // 被缓存的 prompt token 数
  prompt_cache_hit_tokens?: number;   // 缓存命中数
  prompt_cache_miss_tokens?: number;  // 缓存未命中数
  cache_hit_rate?: number;            // 缓存命中率(0~1)
}

export interface ToolStart extends BaseEvent {
  type: "tool_start";
  call_id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolComplete extends BaseEvent {
  type: "tool_complete";
  call_id: string;
  name: string;
  result: string;
  duration_seconds: number;
  is_error: boolean;
}

export interface RoundStart extends BaseEvent {
  type: "round_start";
  round_idx: number;
  max_rounds: number;
}

export interface RoundEnd extends BaseEvent {
  type: "round_end";
  round_idx: number;
  has_tool_calls: boolean;
  final: boolean;
}

export interface ContextWindowUpdated extends BaseEvent {
  type: "context_window_updated";
  context_window: ContextWindow | null;
  reason?: string;
}

export interface Done extends BaseEvent {
  type: "done";
  final_answer: string;
  rounds_used: number;
  cancelled: boolean;
  /** 当前 active 会话的动态上下文窗口估算。 */
  context_window?: ContextWindow | null;
  /** 本轮是否因为上下文接近阈值而自动 compact 或压缩 tool result。 */
  auto_compact?: AutoCompactPayload | null;
}

export interface ErrorEvent extends BaseEvent {
  type: "error";
  where: string;
  message: string;
  exception_type?: string;
}

export interface Cancelled extends BaseEvent {
  type: "cancelled";
  where: string;
}

export interface GatewayReady extends BaseEvent {
  type: "gateway_ready";
  model: string;
  /** 后端当前 active 的本地会话摘要；未启用本地存储时可能为空。 */
  session?: SessionSummary | null;
  /** 后端启动时从 active session 恢复出的普通 history。 */
  history?: RestoredHistoryMessage[];
  usage?: SessionUsage;
  /** 启动恢复后当前会话的上下文窗口估算。 */
  context_window?: ContextWindow | null;
  plan_state?: PlanState | null;
  subagent_tasks?: SubagentTaskSnapshot[];
  permission_mode?: PermissionMode;
}

export type PlanMode = "execute" | "plan";
export type PermissionMode = "request_approval" | "full_access";
export type PlanStatus = "idle" | "pending" | "approved" | "rejected";

export interface PlanState {
  plan_id?: string;
  mode: PlanMode;
  status: PlanStatus;
  revision: number;
  pending_revision?: number | null;
  approved_revision?: number | null;
  current_path?: string | null;
  approved_path?: string | null;
  last_feedback?: string;
  updated_at?: string;
  pending_plan?: string;
  approved_plan?: string;
  pending_plan_preview?: string;
  approved_plan_preview?: string;
}

export interface PlanModeChanged extends BaseEvent {
  type: "plan_mode_changed";
  mode: PlanMode;
  plan_state: PlanState;
}

export interface PermissionModeChanged extends BaseEvent {
  type: "permission_mode_changed";
  permission_mode: PermissionMode;
}

export interface ModelChanged extends BaseEvent {
  type: "model_changed";
  model: string;
  model_key?: string | null;
  provider?: string | null;
  context_window?: ContextWindow | null;
}

export interface PlanStart extends BaseEvent {
  type: "plan_start";
}

export interface PlanDelta extends BaseEvent {
  type: "plan_delta";
  delta: string;
  accumulated: string;
}

export interface PlanReady extends BaseEvent {
  type: "plan_ready";
  plan: string;
  plan_state: PlanState;
}

export interface PlanApproved extends BaseEvent {
  type: "plan_approved";
  plan: string;
  plan_state: PlanState;
}

export interface PlanRejected extends BaseEvent {
  type: "plan_rejected";
  feedback: string;
  plan_state: PlanState;
}

export interface AskQuestionOption {
  label: string;
  description: string;
}

export interface AskUserQuestion extends BaseEvent {
  type: "ask_user_question";
  question_id: string;
  question: string;
  options: AskQuestionOption[];
  multi_select: boolean;
  recommended_index?: number | null;
  allow_other: boolean;
}

export interface AskUserQuestionAnswered extends BaseEvent {
  type: "ask_user_question_answered";
  question_id: string;
  selected_labels: string[];
  other_text?: string | null;
  cancelled: boolean;
}

export interface TodoItem {
  id: string;
  content: string;
  status: "pending" | "in_progress" | "completed" | "cancelled";
}

export interface TodoListUpdated extends BaseEvent {
  type: "todo_list_updated";
  items: TodoItem[];
}

export interface MCPServerStatus {
  name: string;
  transport?: "stdio" | "http" | "sse" | string;
  status: "pending" | "connecting" | "connected" | "error" | "disabled" | string;
  tools_count?: number;
  elapsed_seconds?: number;
  error?: string | null;
}

export interface MCPStatusPayload {
  status: "pending" | "loading" | "ready" | "error" | "disabled" | string;
  servers: MCPServerStatus[];
  total: number;
  connected: number;
  failed: number;
  error?: string | null;
}

export interface MCPStatusEvent extends BaseEvent, MCPStatusPayload {
  type: "mcp_status";
}

/** prompt.submit 的附件输入。后端会重新校验路径、格式、大小和 OCR/ASR/文档转换能力。 */
export interface PromptAttachmentInput {
  path: string;
  modality?: "image" | "audio" | "text" | "document";
  source?: "direct" | "clipboard" | "ocr" | "asr";
}

/** TUI 内部附件队列项。id 只服务前端列表渲染和 detach，不提交给后端。 */
export interface QueuedAttachment extends PromptAttachmentInput {
  id: string;
  fileName: string;
  size?: number | null;
}

export interface SubagentStarted extends BaseEvent {
  type: "subagent_started";
  subagent_id: string;
  subagent_type: string;
  description: string;
  task_id?: string | null;
  run_in_background: boolean;
  parent_session_id?: string | null;
  status?: string;
  phase?: string;
}

export interface SubagentProgress extends BaseEvent {
  type: "subagent_progress";
  subagent_id: string;
  subagent_type: string;
  message: string;
  task_id?: string | null;
  parent_session_id?: string | null;
  status: string;
  phase: string;
  event_seq: number;
  tool_name?: string;
  tool_call_id?: string;
  arguments_preview?: Record<string, unknown>;
  tool_uses?: number;
  active_tool_count?: number;
  total_tokens?: number;
}

export interface SubagentCompleted extends BaseEvent {
  type: "subagent_completed";
  subagent_id: string;
  subagent_type: string;
  description: string;
  status: string;
  content: string;
  task_id?: string | null;
  parent_session_id?: string | null;
  output_path?: string | null;
  duration_seconds: number;
  rounds_used: number;
  is_error: boolean;
}

export interface HookScopeFields {
  hook_call_id: string;
  agent_scope: "root" | "subagent" | string;
  subagent_id?: string | null;
  subagent_type?: string | null;
  parent_session_id?: string | null;
  task_id?: string | null;
  run_in_background?: boolean;
}

export interface HookStarted extends BaseEvent, HookScopeFields {
  type: "hook_started";
  event_name: string;
  handler_type: string;
  matcher: string;
}

export interface HookCompleted extends BaseEvent, HookScopeFields {
  type: "hook_completed";
  event_name: string;
  blocked: boolean;
  has_context: boolean;
  duration_seconds: number;
}

export type AgentEvent =
  | TextDelta
  | ReasoningDelta
  | TokenUsage
  | ToolStart
  | ToolComplete
  | RoundStart
  | RoundEnd
  | ContextWindowUpdated
  | Done
  | ErrorEvent
  | Cancelled
  | GatewayReady
  | AskUserQuestion
  | AskUserQuestionAnswered
  | TodoListUpdated
  | MCPStatusEvent
  | PermissionModeChanged
  | ModelChanged
  | SubagentStarted
  | SubagentProgress
  | SubagentCompleted
  | HookStarted
  | HookCompleted
  | PlanModeChanged
  | PlanStart
  | PlanDelta
  | PlanReady
  | PlanApproved
  | PlanRejected
  | BaseEvent;  // 兜底，未识别的事件不崩溃

// ========== UI 内部状态 ==========

/** 后端 LocalSessionStore 暴露给 UI 的轻量会话摘要。 */
export interface SessionSummary {
  session_id: string;
  created_at?: string;
  updated_at?: string;
  turn_count?: number;
  active_task?: string;
  rolling_summary?: string;
  is_active?: boolean;
}

/** 切换/恢复会话时返回的普通历史消息。 */
export interface RestoredHistoryMessage {
  role: "user" | "assistant" | "system" | string;
  content: string;
  kind?: string | null;
  interrupted?: boolean;
  tool?: {
    name?: string;
    call_id?: string;
    is_error?: boolean;
  } | null;
}

/** 后端估算的当前 active 会话上下文窗口占用。 */
export interface ContextWindow {
  used_tokens: number;
  /** 模型声明的完整上下文窗口。 */
  max_tokens: number;
  full_window_tokens?: number;
  remaining_tokens?: number;
  percent: number;
  model_max_tokens?: number;
  max_output_tokens?: number;
  estimation_margin_tokens?: number;
  soft_limit_tokens?: number;
  hard_limit_tokens?: number;
  raw_estimated_tokens?: number;
  calibration_ratio?: number;
  auto_compact_trigger_tokens?: number;
  auto_compact_trigger_percent?: number;
  source?: string;
  scope?: string;
}

/** 当前主会话跨请求累计的 provider Usage。 */
export interface SessionUsage {
  prompt_tokens: number;
  completion_tokens: number;
  cached_prompt_tokens: number;
  cache_miss_tokens: number;
  requests: number;
  updated_at?: string;
}

/** 自动 compact 的单次审计事件。字段保持宽松，便于后端演进。 */
export interface AutoCompactEvent {
  reason: string;
  round_idx?: number;
  before_messages?: number;
  after_messages?: number;
  before_tokens?: number;
  after_tokens?: number;
  budget_tokens?: number;
  trigger_tokens?: number;
  request_tokens?: number | null;
  compressed_tool_messages?: number;
  persisted?: boolean;
  history_compaction?: AutoCompactEvent | null;
}

export interface AutoCompactPayload {
  compacted: boolean;
  events: AutoCompactEvent[];
}

/** session.create / session.switch 的统一返回形状。 */
export interface SessionPayload {
  session: SessionSummary | null;
  history: RestoredHistoryMessage[];
  context_window?: ContextWindow | null;
  usage?: SessionUsage;
  plan_state?: PlanState | null;
  subagent_tasks?: SubagentTaskSnapshot[];
}

export interface SubagentTaskSnapshot {
  id: string;
  subagent_id: string;
  subagent_type: string;
  description: string;
  status: string;
  phase: string;
  rounds_used?: number;
  tool_uses?: number;
  active_tool_count?: number;
  total_tokens?: number;
  event_seq?: number;
  duration_seconds?: number | null;
  output_path?: string;
  result_preview?: string;
  error?: string;
  current_tool?: {
    name?: string;
    arguments?: Record<string, unknown>;
  } | null;
}

/** session.compact 的返回形状。 */
export interface CompactPayload extends SessionPayload {
  summary: string;
  before_messages: number;
  after_messages: number;
  persisted: boolean;
  no_op?: boolean;
}

export interface ModelChoice {
  key: string;
  provider: string;
  model: string;
  name: string;
  current?: boolean;
  is_tool?: boolean;
  is_reasoning?: boolean;
  max_tokens?: number;
  max_output_tokens?: number;
  output_token_param?: "max_tokens" | "max_completion_tokens" | "none";
  image_ability?: boolean;
  base_url?: string;
}

export interface ModelListPayload {
  models: ModelChoice[];
  current?: {
    key?: string | null;
    model?: string | null;
    base_url?: string | null;
    is_tool?: boolean;
    is_reasoning?: boolean;
    max_tokens?: number;
    max_output_tokens?: number;
    output_token_param?: "max_tokens" | "max_completion_tokens" | "none";
    image_ability?: boolean;
  };
  config_path?: string | null;
}

export interface ModelSwitchPayload {
  model: ModelChoice;
  context_window?: ContextWindow | null;
}

export interface CacheStatsBucket {
  model?: string | null;
  requests: number;
  supported_requests: number;
  unsupported_requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cache_hit_tokens: number;
  cache_denominator_tokens: number;
  cache_hit_rate: number | null;
}

export interface CacheStatsPayload {
  date: string;
  path: string;
  total: CacheStatsBucket;
  models: CacheStatsBucket[];
}

export type Role = "user" | "assistant" | "tool" | "system" | "ask_question" | "todo" | "thought" | "plan" | "subagent";
/** 对话流里渲染的一项。一个 chat round 通常会产生多个 item。 */
export interface ChatItem {
  id: string;
  role: Role;
  text: string;             // user / assistant 用
  toolCallId?: string;      // tool item 用：后端 call_id，精确配对 tool_complete
  toolName?: string;        // tool item 用
  toolArgs?: Record<string, unknown>;
  toolResult?: string;
  toolDuration?: number;
  toolError?: boolean;
  toolDone?: boolean;       // false=运行中，true=已完成
  collapsed?: boolean;      // 工具块默认折叠
  // 问答（ask_question role 用）
  questionId?: string;
  question?: string;
  options?: AskQuestionOption[];
  multiSelect?: boolean;
  recommendedIndex?: number | null;
  allowOther?: boolean;
  answered?: boolean;       // 用户已作答；面板转为静态摘要
  answerLabels?: string[];
  answerOther?: string;
  answerCancelled?: boolean;
  // todo（todo role 用）：每次 todo 写入产生一个新 item，items 是该次写入后的全量列表
  todoItems?: TodoItem[];
  planStatus?: PlanStatus;
  planRevision?: number | null;
  // 子代理任务面板：同一个 taskId 的 started/progress/completed 事件原地更新。
  subagentId?: string;
  subagentType?: string;
  subagentTaskId?: string;
  subagentDescription?: string;
  subagentStatus?: string;
  subagentPhase?: string;
  subagentMessage?: string;
  subagentEventSeq?: number;
  subagentToolName?: string;
  subagentToolArgs?: Record<string, unknown>;
  subagentToolUses?: number;
  subagentActiveTools?: number;
  subagentTokens?: number;
  subagentRounds?: number;
  subagentDuration?: number;
  subagentOutputPath?: string;
  subagentError?: boolean;
}

// ========== 浮层 Select 弹窗 ==========

/** 浮层弹窗里的一个可选项。 */
export interface DialogOption {
  /** 主标题（一行）。 */
  name: string;
  /** 副描述（灰色，可选）。 */
  description?: string;
  /** 选中时回传给 onSelect 的值。 */
  value: string;
}

/** session.list_skills 返回的 Skill 索引项。 */
export interface SkillSummary {
  name: string;
  description?: string;
  short_description?: string | null;
  path?: string;
}

/** 浮层 Select 弹窗描述。/sessions /tools /mcp 等命令用它开小窗而非往对话流打印。 */
export interface DialogSpec {
  title: string;
  options?: DialogOption[];
  /** Read-only content panel. If set, SelectDialog shows this instead of options. */
  content?: string;
  /** Number of options visible at once; SelectDialog scrolls for the rest. */
  visibleCount?: number;
  /** 选中回调；弹窗在调用前已关闭。空列表时弹窗只展示标题与提示。 */
  onSelect?: (value: string) => void;
}
