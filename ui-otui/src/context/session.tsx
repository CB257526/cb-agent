/**
 * SessionProvider：全局会话状态 + AgentEvent → 状态 reducer。
 *
 * 取代旧 ui-tui App.tsx 里几十个 useState + 自适应节流逻辑。核心简化：
 *
 *   旧实现要靠 adaptiveDelay/scheduleFlush（60→200ms 节流）和 windowing（只渲染 50 条）
 *   来压制 Ink "每次流式 chunk 都全树重渲 → 终端抖动 + 滚轮跳顶" 的缺陷。
 *
 *   OpenTUI 的 <scrollbox> 有真实独立视口（滚动状态与终端 scrollback 解耦），
 *   Solid 的 createStore 又是细粒度更新（只重绘变化的那条 item）。两者叠加后，
 *   流式高频 text_delta 直接 setStore 追加即可，无需任何节流/窗口化。
 *
 * 事件映射规则沿用旧实现语义（见各 case 注释）。
 */

import {
  createContext,
  useContext,
  onMount,
  onCleanup,
  type ParentProps,
} from "solid-js";
import { createStore, produce } from "solid-js/store";
import { useTransport } from "./transport.js";
import { findCommand, makeQueuedAttachment, type SlashCommand, type CommandCtx } from "../commands.js";
import { readClipboardForPaste } from "../clipboardImage.js";
import { HistoryStore } from "../historyStore.js";
import type {
  AgentEvent,
  ChatItem,
  ContextWindow,
  MCPStatusPayload,
  SessionSummary,
  TodoItem,
  PetState,
  QueuedAttachment,
  PromptAttachmentInput,
  DialogSpec,
  PlanMode,
  PlanState,
  RestoredHistoryMessage,
  SessionPayload,
} from "../types.js";

const STDERR_RING_MAX = 200;

// 命令历史：进程内只加载一次
const historyStore = new HistoryStore();
historyStore.load();

let _idCounter = 0;
const nextId = () => `i${++_idCounter}`;

export interface SessionState {
  items: ChatItem[];
  busy: boolean;
  planState: PlanState | null;
  model: string;
  promptTokens: number;
  completionTokens: number;
  cachedPromptTokens: number;
  contextWindow: ContextWindow | null;
  round: number;
  maxRounds: number;
  mcp: MCPStatusPayload | null;
  todos: TodoItem[];
  session: SessionSummary | null;
  activeQuestionId: string | null;
  stderrLines: string[];
  showActivity: boolean;
  exited: boolean;
  /** 待随下一条 prompt 一起提交的附件队列；发送成功后清空。 */
  attachments: QueuedAttachment[];
  /** 桌宠状态，供 Sidebar 展示和 /pet 命令同步。 */
  pet: PetState | null;
  /** 命令级长操作（如 /compact 调 LLM 摘要）进行中：驱动 Footer 动效，区别于 busy。 */
  pending: string | null;
  /** 当前打开的浮层 Select 弹窗；null 表示无。供 /sessions /tools /mcp 等命令使用。 */
  dialog: DialogSpec | null;
}

/** 后端恢复的普通 history → ChatItem。工作记录/压缩记录降级为 system 行展示。 */
function restoredHistoryToItems(history: RestoredHistoryMessage[]): ChatItem[] {
  return history
    .filter((m) => m.content)
    .map((m) => {
      if (
        m.kind === "work_record" ||
        m.kind === "compact_record" ||
        m.content.startsWith("【工作记录】") ||
        m.content.startsWith("【上下文压缩】")
      ) {
        return { id: nextId(), role: "system", text: m.content } as ChatItem;
      }
      if (m.role === "user") return { id: nextId(), role: "user", text: m.content } as ChatItem;
      if (m.role === "assistant") return { id: nextId(), role: "assistant", text: m.content } as ChatItem;
      return { id: nextId(), role: "system", text: m.content } as ChatItem;
    });
}

function planStateToItem(state: PlanState | null | undefined): ChatItem | null {
  if (!state) return null;
  const status = state.status ?? "idle";
  const text =
    status === "approved"
      ? (state.approved_plan || state.approved_plan_preview || "")
      : (state.pending_plan || state.pending_plan_preview || "");
  if (!text.trim()) return null;
  return {
    id: nextId(),
    role: "plan",
    text,
    planStatus: status,
    planRevision: status === "approved"
      ? (state.approved_revision ?? state.revision ?? null)
      : (state.pending_revision ?? state.revision ?? null),
  };
}

function formatCompactTokens(tokens: unknown): string {
  if (typeof tokens !== "number" || !Number.isFinite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`;
  return `${Math.round(tokens)}`;
}

function describeAutoCompact(e: any): string | null {
  const payload = e.auto_compact;
  if (!payload?.compacted || !Array.isArray(payload.events) || payload.events.length === 0) {
    return null;
  }
  const compressedToolMessages = payload.events.reduce((sum: number, item: any) => {
    return sum + Number(item?.compressed_tool_messages || 0);
  }, 0);
  const historyEvents = payload.events.filter((item: any) => {
    if (!item) return false;
    if (item.reason && item.reason !== "tool_loop") return true;
    return !!item.history_compaction;
  });
  const context = e.context_window;
  const contextText = context
    ? `Context ${formatCompactTokens(context.used_tokens)}/${formatCompactTokens(context.max_tokens)} ${context.percent ?? 0}%`
    : "Context refreshed";
  const parts: string[] = [];
  if (compressedToolMessages > 0) parts.push(`tool results ${compressedToolMessages}`);
  if (historyEvents.length > 0) parts.push(`history ${historyEvents.length}`);
  if (parts.length === 0) parts.push("context guard");
  return `已自动压缩上下文：${parts.join("，")}，${contextText}。`;
}

interface SessionContextValue {
  state: SessionState;
  /** 提交用户输入：斜杠命令拦截执行，否则追加 user item + 置 busy + 发 prompt.submit。 */
  submit: (text: string) => void;
  /** 追加一行 system 提示（命令反馈用）。 */
  appendSystem: (text: string) => void;
  /** 用全量列表替换 items（/clear、会话切换用）。 */
  setItems: (items: ChatItem[]) => void;
  /** 执行斜杠命令（命令面板选中时调）。 */
  runCommand: (cmd: SlashCommand, commandLine?: string) => void;
  /** 切换后端日志面板。 */
  toggleActivity: () => void;
  /** ↑/↓ 翻历史：idx 0 = 最新，递增 = 更老；越界返回 null。 */
  getHistoryAt: (idx: number) => string | null;
  /** 回答 AskUserQuestionTool 的提问。 */
  answerQuestion: (questionId: string, params: { selected_labels: string[]; other_text?: string; cancelled?: boolean }) => void;
  /** 维护附件队列（命令 / 粘贴用）。 */
  setAttachments: (updater: (prev: QueuedAttachment[]) => QueuedAttachment[]) => void;
  /** 从系统剪贴板读取文本/文件/图片：文本插入输入框，文件/图片进附件队列。 */
  pasteFromClipboard: (insertText: (text: string) => void) => void;
  /** 更新桌宠状态（/pet 命令用）。 */
  setPet: (pet: PetState | null) => void;
  /** 打开浮层 Select 弹窗。 */
  openDialog: (spec: DialogSpec) => void;
  /** 关闭浮层弹窗。 */
  closeDialog: () => void;
  /** Set Plan/Execute mode. */
  setPlanMode: (mode: PlanMode) => Promise<void>;
  /** Toggle Plan/Execute mode. */
  togglePlanMode: () => void;
  /** Update local Plan state from RPC/events. */
  setPlanState: (state: PlanState | null) => void;
}

const SessionContext = createContext<SessionContextValue>();

export function SessionProvider(props: ParentProps) {
  const transport = useTransport();

  const [state, setState] = createStore<SessionState>({
    items: [],
    busy: false,
    planState: null,
    model: "connecting…",
    promptTokens: 0,
    completionTokens: 0,
    cachedPromptTokens: 0,
    contextWindow: null,
    round: 0,
    maxRounds: 0,
    mcp: null,
    todos: [],
    session: null,
    activeQuestionId: null,
    stderrLines: [],
    showActivity: false,
    exited: false,
    attachments: [],
    pet: null,
    pending: null,
    dialog: null,
  });

  // prompt.submit 是 fire-and-forget，但后端会立刻回一个 accepted/error。
  // 附件队列等这个 ack 后再清空，避免 submit 被拒时用户得重挑文件。
  let pendingSubmitId: string | null = null;
  let streamingPlanId: string | null = null;

  // 本次 chat 是否产生过任何可见输出（文本/思考/工具调用）。submit 时复位，
  // 收到 text_delta/reasoning_delta/tool_start 置 true；done 时若仍为 false，
  // 说明模型返回了空响应（completion_tokens=0），给用户一条提示而非静默无反应。
  let sawOutput = false;

  const appendItem = (item: ChatItem) =>
    setState("items", (prev) => [...prev, item]);

  const appendSystem = (text: string) =>
    appendItem({ id: nextId(), role: "system", text });

  /** text_delta：追加到末尾 assistant item；末尾不是 assistant 则新建一条。 */
  const appendAssistantText = (delta: string) => {
    setState(
      produce((s) => {
        const last = s.items[s.items.length - 1];
        if (last && last.role === "assistant") {
          last.text += delta;
        } else {
          s.items.push({ id: nextId(), role: "assistant", text: delta });
        }
      }),
    );
  };

  /** reasoning_delta：追加到末尾 thought item；末尾不是 thought 则新建一条。 */
  const appendThoughtText = (delta: string) => {
    setState(
      produce((s) => {
        const last = s.items[s.items.length - 1];
        if (last && last.role === "thought") {
          last.text += delta;
        } else {
          s.items.push({ id: nextId(), role: "thought", text: delta });
        }
      }),
    );
  };

  const appendPlanText = (delta: string) => {
    setState(
      produce((s) => {
        if (streamingPlanId) {
          const existing = s.items.find((it) => it.id === streamingPlanId);
          if (existing) {
            existing.text += delta;
            return;
          }
        }
        streamingPlanId = nextId();
        s.items.push({
          id: streamingPlanId,
          role: "plan",
          text: delta,
          planStatus: "idle",
          planRevision: null,
        });
      }),
    );
  };

  const onEvent = (ev: AgentEvent) => {
    const e = ev as any;
    switch (ev.type) {
      case "gateway_ready":
        setState("model", e.model ?? "unknown");
        setState("session", e.session ?? null);
        if (e.context_window !== undefined) setState("contextWindow", e.context_window ?? null);
        if (e.plan_state !== undefined) setState("planState", e.plan_state ?? null);
        {
          const notice = describeAutoCompact(e);
          if (notice) appendSystem(notice);
        }
        if (Array.isArray(e.history)) {
          const restored = restoredHistoryToItems(e.history);
          const planItem = planStateToItem(e.plan_state);
          if (planItem) restored.push(planItem);
          if (restored.length > 0) setState("items", restored);
        }
        break;

      case "plan_mode_changed":
        setState("planState", e.plan_state ?? null);
        break;

      case "plan_start":
        streamingPlanId = null;
        break;

      case "plan_delta":
        sawOutput = true;
        appendPlanText(e.delta as string);
        break;

      case "plan_ready":
        sawOutput = true;
        setState("planState", e.plan_state ?? null);
        setState(
          produce((s) => {
            const id = streamingPlanId;
            streamingPlanId = null;
            if (!id) {
              s.items.push({
                id: nextId(),
                role: "plan",
                text: String(e.plan ?? ""),
                planStatus: "pending",
                planRevision: e.plan_state?.pending_revision ?? e.plan_state?.revision ?? null,
              });
              return;
            }
            const item = s.items.find((it) => it.id === id);
            if (item) {
              item.text ||= String(e.plan ?? "");
              item.planStatus = "pending";
              item.planRevision = e.plan_state?.pending_revision ?? e.plan_state?.revision ?? null;
            }
          }),
        );
        break;

      case "plan_approved":
        setState("planState", e.plan_state ?? null);
        setState(
          produce((s) => {
            for (const item of s.items) {
              if (item.role === "plan" && item.planStatus === "pending") {
                item.planStatus = "approved";
                item.planRevision = e.plan_state?.approved_revision ?? item.planRevision;
              }
            }
          }),
        );
        appendSystem("Plan approved. Switched back to EXEC mode.");
        break;

      case "plan_rejected":
        setState("planState", e.plan_state ?? null);
        setState(
          produce((s) => {
            for (const item of s.items) {
              if (item.role === "plan" && item.planStatus === "pending") {
                item.planStatus = "rejected";
              }
            }
          }),
        );
        appendSystem("Plan rejected. Feedback will be included in the next Plan Mode turn.");
        break;

      case "round_start":
        setState("round", e.round_idx);
        setState("maxRounds", e.max_rounds);
        break;

      case "text_delta":
        sawOutput = true;
        appendAssistantText(e.delta as string);
        break;

      case "reasoning_delta":
        sawOutput = true;
        appendThoughtText(e.delta as string);
        break;

      case "tool_start":
        sawOutput = true;
        // 按 call_id 配对（修掉旧实现"按 name + 最近未完成"匹配的隐患）
        appendItem({
          id: nextId(),
          role: "tool",
          text: "",
          toolCallId: e.call_id,
          toolName: e.name,
          toolArgs: e.arguments,
          toolDone: false,
          collapsed: true,
        });
        break;

      case "tool_complete":
        setState(
          produce((s) => {
            // 优先用 call_id 精确配对；找不到再退回同名最近未完成项
            let idx = s.items.findIndex(
              (it) => it.role === "tool" && !it.toolDone && it.toolCallId === e.call_id,
            );
            if (idx < 0) {
              for (let i = s.items.length - 1; i >= 0; i--) {
                const it = s.items[i];
                if (it.role === "tool" && !it.toolDone && it.toolName === e.name) {
                  idx = i;
                  break;
                }
              }
            }
            if (idx >= 0) {
              const it = s.items[idx];
              it.toolResult = e.result;
              it.toolDuration = e.duration_seconds;
              it.toolError = e.is_error;
              it.toolDone = true;
            }
          }),
        );
        break;

      case "token_usage":
        setState("promptTokens", (p) => p + (e.prompt_tokens ?? 0));
        setState("completionTokens", (c) => c + (e.completion_tokens ?? 0));
        setState("cachedPromptTokens", (c) => c + (e.cached_prompt_tokens ?? e.prompt_cache_hit_tokens ?? 0));
        break;

      case "todo_list_updated":
        setState("todos", e.items ?? []);
        // 沿用旧偏好：每次写入新增一张快照卡片，不去重
        appendItem({ id: nextId(), role: "todo", text: "", todoItems: e.items ?? [] });
        break;

      case "mcp_status":
        setState("mcp", { ...e } as MCPStatusPayload);
        break;

      case "pet_updated":
        setState("pet", e.state ?? null);
        break;

      case "ask_user_question":
        appendItem({
          id: nextId(),
          role: "ask_question",
          text: "",
          questionId: e.question_id,
          question: e.question,
          options: e.options,
          multiSelect: e.multi_select,
          recommendedIndex: e.recommended_index,
          allowOther: e.allow_other,
          answered: false,
        });
        setState("activeQuestionId", e.question_id);
        break;

      case "ask_user_question_answered":
        setState(
          produce((s) => {
            for (let i = s.items.length - 1; i >= 0; i--) {
              const it = s.items[i];
              if (it.role === "ask_question" && it.questionId === e.question_id) {
                it.answered = true;
                it.answerLabels = e.selected_labels ?? [];
                it.answerOther = e.other_text ?? undefined;
                it.answerCancelled = !!e.cancelled;
                break;
              }
            }
            if (s.activeQuestionId === e.question_id) s.activeQuestionId = null;
          }),
        );
        break;

      case "context_window_updated":
        if (e.context_window !== undefined) setState("contextWindow", e.context_window ?? null);
        break;

      case "done":
        if (e.context_window !== undefined) setState("contextWindow", e.context_window ?? null);
        // 整轮没有任何文本/思考/工具输出 = 模型空响应。提示可能原因，避免用户以为 UI 卡了。
        // 被取消的轮次不算空响应（用户主动中断），跳过提示。
        if (!sawOutput && !e.cancelled) {
          appendSystem(
            "模型返回了空响应（completion_tokens=0，无文本/工具调用）。" +
              "常见原因：注册的工具过多导致请求过大、某个 MCP 工具 schema 不合法，或上游接口异常。" +
              "可尝试 /compact 压缩上下文，或关闭部分 MCP 后重试。",
          );
        }
        setState("busy", false);
        setState("round", 0);
        break;

      case "error":
        appendSystem(`✗ ${e.where}: ${e.message}`);
        setState("busy", false);
        break;

      case "cancelled":
        appendSystem(`⏸ 已中断 (${e.where})`);
        setState("busy", false);
        break;

      default:
        break;
    }
  };

  const onStderr = (line: string) => {
    setState("stderrLines", (prev) => {
      const next = prev.length >= STDERR_RING_MAX ? prev.slice(-STDERR_RING_MAX + 1) : prev.slice();
      next.push(line);
      return next;
    });
  };

  // prompt.submit 的 ack：成功则清空附件队列；失败则解除 busy 并提示。
  const onResponse = (
    id: string | number,
    body: { result?: unknown; error?: { code: number; message: string } },
  ) => {
    if (id !== pendingSubmitId) return;
    pendingSubmitId = null;
    if (body.error) {
      setState("busy", false);
      appendSystem(`提交失败：${body.error.message}`);
      return;
    }
    setState("attachments", []);
  };

  const onProtoErr = (_raw: string, err: Error) => {
    appendSystem(`协议解析错误：${err.message}（详情见 ${transport.stderrLogFile}）`);
  };

  const onExit = (code: number | null) => {
    appendSystem(`Python agent 进程退出 (code=${code ?? "?"})`);
    setState("exited", true);
  };

  onMount(() => {
    transport.on("event", onEvent);
    transport.on("response", onResponse);
    transport.on("stderr", onStderr);
    transport.on("protocolError", onProtoErr);
    transport.on("exit", onExit);
    // 拉取桌宠初始状态（失败静默：宠物是附属功能，不该阻塞 UI）
    transport
      .getPetState()
      .then((pet) => setState("pet", pet ?? null))
      .catch(() => setState("pet", null));
  });

  onCleanup(() => {
    transport.removeListener("event", onEvent);
    transport.removeListener("response", onResponse);
    transport.removeListener("stderr", onStderr);
    transport.removeListener("protocolError", onProtoErr);
    transport.removeListener("exit", onExit);
  });

  const setAttachments = (updater: (prev: QueuedAttachment[]) => QueuedAttachment[]) =>
    setState("attachments", (prev) => updater(prev));

  /** 切换/新建会话返回的 payload → 重绘对话流：恢复 history + 同步 session/上下文窗口。 */
  const applySessionPayload = (payload: SessionPayload) => {
    const restored = restoredHistoryToItems(payload.history ?? []);
    const planItem = planStateToItem(payload.plan_state);
    if (planItem) restored.push(planItem);
    setState("items", restored);
    setState("session", payload.session ?? null);
    if (payload.plan_state !== undefined) setState("planState", payload.plan_state ?? null);
    streamingPlanId = null;
    setState("promptTokens", 0);
    setState("completionTokens", 0);
    setState("cachedPromptTokens", 0);
    if (payload.context_window !== undefined) setState("contextWindow", payload.context_window ?? null);
    // 切会话即结束上一会话的流式态，清空 round / busy，防止残留动效
    setState("busy", false);
    setState("round", 0);
    setState("todos", []);
    setState("activeQuestionId", null);
  };

  const setPlanMode = async (mode: PlanMode) => {
    try {
      const payload = await transport.setMode(mode);
      setState("planState", payload.plan_state ?? null);
    } catch (e) {
      appendSystem(`Plan mode switch failed: ${(e as Error).message}`);
    }
  };

  const togglePlanMode = () => {
    const current = state.planState?.mode ?? "execute";
    void setPlanMode(current === "plan" ? "execute" : "plan");
  };

  const runCommand = (cmd: SlashCommand, commandLine?: string) => {
    const line = (commandLine ?? cmd.name).trim();
    const ctx: CommandCtx = {
      transport,
      input: line,
      args: line.slice(cmd.name.length).trim(),
      appendSystem,
      setItems: (items) => setState("items", items),
      resetSessionStats: () => {
        setState("promptTokens", 0);
        setState("completionTokens", 0);
        setState("cachedPromptTokens", 0);
        setState("contextWindow", (prev) =>
          prev
            ? {
                ...prev,
                used_tokens: 0,
                remaining_tokens: prev.max_tokens,
                percent: 0,
              }
            : prev,
        );
      },
      setContextWindow: (contextWindow) => setState("contextWindow", contextWindow),
      toggleActivity: () => setState("showActivity", (v) => !v),
      attachments: state.attachments,
      setAttachments,
      setPet: (pet) => setState("pet", pet),
      setPlanState: (planState) => setState("planState", planState),
      setPlanMode,
      setPending: (label) => setState("pending", label),
      openDialog: (spec) => setState("dialog", spec),
      applySessionPayload,
    };
    const ret = cmd.handler(ctx);
    if (ret instanceof Promise) {
      ret
        .catch((e) => appendSystem(`✗ 命令 ${cmd.name} 抛错：${(e as Error).message}`))
        .finally(() => setState("pending", null)); // 兜底清 pending，防命令忘清或抛错残留
    }
  };

  const submit = (text: string) => {
    const pending = state.attachments;
    if ((!text.trim() && pending.length === 0) || state.busy) return;
    // 斜杠命令：拦截，不走 prompt.submit，也不入历史
    if (text.startsWith("/")) {
      const cmd = findCommand(text);
      if (cmd) runCommand(cmd, text);
      else appendSystem(`未知命令：${text.split(/\s+/)[0]}（输入 / 查看可用命令）`);
      return;
    }
    if (text.trim()) historyStore.push(text);

    // user item 文本带上附件清单，让用户在对话流里看到这条消息带了哪些文件
    const attachmentLines = pending.map((item, i) => `  ${i + 1}. ${item.fileName} (${item.source ?? "direct"})`);
    const displayText = [
      text.trim() || "请根据附件回答。",
      attachmentLines.length ? "附件：\n" + attachmentLines.join("\n") : "",
    ]
      .filter(Boolean)
      .join("\n\n");
    const submitAttachments: PromptAttachmentInput[] = pending.map(({ path, modality, source }) => ({
      path,
      modality,
      source,
    }));

    appendItem({ id: nextId(), role: "user", text: displayText });
    setState("busy", true);
    sawOutput = false; // 新一轮：复位"是否产生过输出"，done 时据此判定空响应
    // 记录这次 submit 的 RPC id：ack 回来后再清空附件队列（见 onResponse）
    pendingSubmitId = transport.sendPrompt(text, submitAttachments);
  };

  /** Ctrl-V 粘贴：文本插回输入框，文件/图片加入附件队列。 */
  let pasting = false; // 防重入：一次读剪贴板要 spawn PowerShell（数百 ms），连按会并发堆叠堵死事件循环
  const pasteFromClipboard = (insertText: (text: string) => void) => {
    if (state.busy || state.activeQuestionId !== null) return;
    if (pasting) return; // 上一次还没读完，忽略这次按键
    pasting = true;
    readClipboardForPaste()
      .then((item) => {
        if (item.kind === "text") {
          insertText(item.text);
          return;
        }
        if (item.kind === "files") {
          const queued = item.paths.map((path) => makeQueuedAttachment(path, "clipboard"));
          setAttachments((prev) => [...prev, ...queued]);
          appendSystem(`已从剪贴板添加 ${queued.length} 个文件。发送下一条消息时会一起提交。`);
          return;
        }
        setAttachments((prev) => [...prev, item.attachment]);
        appendSystem(`已从剪贴板添加图片：${item.attachment.fileName}`);
      })
      .catch((e) => {
        appendSystem(`剪贴板读取失败：${(e as Error).message}`);
      })
      .finally(() => {
        pasting = false;
      });
  };

  const getHistoryAt = (idx: number): string | null => {
    const all = historyStore.all();
    if (idx < 0 || idx >= all.length) return null;
    return all[all.length - 1 - idx];
  };

  const value: SessionContextValue = {
    state,
    submit,
    appendSystem,
    setItems: (items) => setState("items", items),
    runCommand,
    toggleActivity: () => setState("showActivity", (v) => !v),
    getHistoryAt,
    answerQuestion: (questionId, params) => {
      transport.answerQuestion({ question_id: questionId, ...params });
      // 不立刻清 activeQuestionId：等 ask_user_question_answered 事件回来再清
    },
    setAttachments,
    pasteFromClipboard,
    setPet: (pet) => setState("pet", pet),
    openDialog: (spec) => setState("dialog", spec),
    closeDialog: () => setState("dialog", null),
    setPlanMode,
    togglePlanMode,
    setPlanState: (planState) => setState("planState", planState),
  };

  return <SessionContext.Provider value={value}>{props.children}</SessionContext.Provider>;
}

export function useSession(): SessionContextValue {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession 必须在 SessionProvider 内使用");
  return ctx;
}
