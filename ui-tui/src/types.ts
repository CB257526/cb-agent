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
  | "done"
  | "error"
  | "cancelled"
  | "background_notification"
  | "ask_user_question"
  | "ask_user_question_answered"
  | "todo_list_updated"
  | "mcp_status"
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
  /** 启动恢复后当前会话的上下文窗口估算。 */
  context_window?: ContextWindow | null;
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

export type AgentEvent =
  | TextDelta
  | ReasoningDelta
  | TokenUsage
  | ToolStart
  | ToolComplete
  | RoundStart
  | RoundEnd
  | Done
  | ErrorEvent
  | Cancelled
  | GatewayReady
  | AskUserQuestion
  | AskUserQuestionAnswered
  | TodoListUpdated
  | MCPStatusEvent
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
}

/** 后端估算的当前 active 会话上下文窗口占用。 */
export interface ContextWindow {
  used_tokens: number;
  /** agent 实际使用的安全窗口，默认是模型 max_tokens 的 80%。 */
  max_tokens: number;
  remaining_tokens?: number;
  percent: number;
  /** 模型声明的完整上下文窗口，来自 constant/llm/constant_llm.py。 */
  model_max_tokens?: number;
  /** max_tokens 相对 model_max_tokens 的比例，默认 0.8。 */
  threshold_ratio?: number;
  source?: string;
  scope?: string;
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
}

/** session.compact 的返回形状。 */
export interface CompactPayload extends SessionPayload {
  summary: string;
  before_messages: number;
  after_messages: number;
  persisted: boolean;
  no_op?: boolean;
}

export type Role = "user" | "assistant" | "tool" | "system" | "ask_question" | "todo" | "thought";

/** 对话流里渲染的一项。一个 chat round 通常会产生多个 item。 */
export interface ChatItem {
  id: string;
  role: Role;
  text: string;             // user / assistant 用
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
}
