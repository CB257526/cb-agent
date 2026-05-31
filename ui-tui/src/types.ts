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
  | BaseEvent;  // 兜底，未识别的事件不崩溃

// ========== UI 内部状态 ==========

export type Role = "user" | "assistant" | "tool" | "system";

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
}
