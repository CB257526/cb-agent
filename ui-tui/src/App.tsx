/**
 * cb-agent TUI 主组件。
 *
 * 状态机：把扁平的 AgentEvent 流折叠成结构化的 ChatItem 列表给 EventStream 渲染。
 *
 * 关键映射规则：
 *   user prompt 提交     → 追加 user item
 *   text_delta           → 找最近一个 assistant item 追加文本；没有就新建一个
 *   tool_start           → 新建 tool item（toolDone=false）
 *   tool_complete        → 找到 call_id 对应的 tool item，填 result/duration/done=true
 *   round_start          → 不创建 item，只更新 status
 *   token_usage          → 更新累积 token 统计
 *   done                 → 释放 busy 状态、token 加到累积里
 *   error / cancelled    → 追加 system item
 *
 * busy 状态：发出 prompt 后变 true，收到 done/error/cancelled 时变 false。
 * 期间 PromptInput 禁用，避免用户连发引发 -32001 session busy。
 */

import React, { useEffect, useState, useCallback, useRef } from "react";
import { Box, useApp, useInput } from "ink";
import { Transport } from "./transport.js";
import { AgentEvent, ChatItem, ContextWindow, PetState, RestoredHistoryMessage, SessionPayload, SessionSummary } from "./types.js";
import { EventStream } from "./components/EventStream.js";
import { StatusBar } from "./components/StatusBar.js";
import { PromptInput } from "./components/PromptInput.js";
import { AttachmentQueue } from "./components/AttachmentQueue.js";
import { ActivityPanel } from "./components/ActivityPanel.js";
import { Banner } from "./components/Banner.js";
import { SlashCommandPicker } from "./components/SlashCommandPicker.js";
import { SessionSwitcher } from "./components/SessionSwitcher.js";
import { HistoryStore } from "./historyStore.js";
import { findCommand, SlashCommand, CommandCtx, formatMCPStatus } from "./commands.js";
import { readClipboardImageAttachment } from "./clipboardImage.js";
import type { PromptAttachmentInput, QueuedAttachment } from "./types.js";

const STDERR_RING_MAX = 200;  // 内存里最多留 200 行，超出从头丢

// 单例：历史只在进程内加载一次
const historyStore = new HistoryStore();
historyStore.load();

let _idCounter = 0;
const nextId = () => `i${++_idCounter}`;

function formatCompactTokens(tokens: unknown): string {
  if (typeof tokens !== "number" || !Number.isFinite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`;
  return `${Math.round(tokens)}`;
}

function describeAutoCompact(ev: AgentEvent): string | null {
  const payload = (ev as any).auto_compact;
  if (!payload?.compacted || !Array.isArray(payload.events) || payload.events.length === 0) {
    return null;
  }

  // 自动 compact 是后端为了保护下一轮 prompt 做的维护动作，不应该像手动
  // /compact 一样重绘整段 history。这里仅把审计信息压成一行 system 提示：
  // 用户能知道"发生过压缩"，当前屏幕上的工具卡片和助手回答则保持原样。
  const compressedToolMessages = payload.events.reduce((sum: number, item: any) => {
    return sum + Number(item?.compressed_tool_messages || 0);
  }, 0);
  const historyEvents = payload.events.filter((item: any) => {
    if (!item) return false;
    if (item.reason && item.reason !== "tool_loop") return true;
    return !!item.history_compaction;
  });
  const context = (ev as any).context_window;
  const contextText = context
    ? `Context ${formatCompactTokens(context.used_tokens)}/${formatCompactTokens(context.max_tokens)} ${context.percent ?? 0}%`
    : "Context 已刷新";
  const parts: string[] = [];
  if (compressedToolMessages > 0) parts.push(`压缩 tool 结果 ${compressedToolMessages} 条`);
  if (historyEvents.length > 0) parts.push(`压缩会话记忆 ${historyEvents.length} 次`);
  if (parts.length === 0) parts.push("已执行上下文保护");
  return `已自动压缩上下文：${parts.join("，")}，${contextText}。`;
}

function restoredHistoryToItems(history: RestoredHistoryMessage[]): ChatItem[] {
  return history
    .filter((m) => m.content)
    .map((m) => {
      // 工作记录/上下文压缩记录虽然在后端 history 里是普通 assistant message，
      // 但在 UI 上用 system 行展示更不容易和模型给用户的最终回答混淆。
      if (
        m.kind === "work_record"
        || m.kind === "compact_record"
        || m.content.startsWith("【工作记录】")
        || m.content.startsWith("【上下文压缩】")
      ) {
        return { id: nextId(), role: "system", text: m.content } as ChatItem;
      }
      if (m.role === "user") {
        return { id: nextId(), role: "user", text: m.content } as ChatItem;
      }
      if (m.role === "assistant") {
        return { id: nextId(), role: "assistant", text: m.content } as ChatItem;
      }
      return { id: nextId(), role: "system", text: m.content } as ChatItem;
    });
}

export function App({ transport, clearScreen }: { transport: Transport; clearScreen?: () => void }) {
  const { exit } = useApp();

  const [items, setItems] = useState<ChatItem[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [model, setModel] = useState("connecting…");
  const [round, setRound] = useState(0);
  const [maxRounds, setMaxRounds] = useState(0);
  const [promptTokens, setPromptTokens] = useState(0);
  const [completionTokens, setCompletionTokens] = useState(0);
  const [contextWindow, setContextWindow] = useState<ContextWindow | null>(null);
  const [protocolErrors, setProtocolErrors] = useState(0);
  const [stderrLines, setStderrLines] = useState<string[]>([]);
  const [showActivity, setShowActivity] = useState(false);
  const [currentSession, setCurrentSession] = useState<SessionSummary | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [showSessionSwitcher, setShowSessionSwitcher] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  const [, setPetState] = useState<PetState | null>(null);
  const [attachments, setAttachments] = useState<QueuedAttachment[]>([]);
  // 当前等待用户作答的问题 id：决定 EventStream 把输入路由给哪个 panel；
  // 同时 PromptInput 在问答期 disabled，避免误打字提交 prompt
  const [activeQuestionId, setActiveQuestionId] = useState<string | null>(null);
  // 是否展开全部历史消息（Ctrl+E 切换）。默认折叠（只显示最近 50 条）防抖动。
  const [showAllMessages, setShowAllMessages] = useState(false);

  // / 命令面板：只在输入"纯命令名前缀"时显示。带参数的命令（如
  // /switch <session_id>）需要让 Enter 直接提交给 handleSubmit，否则 picker
  // 会抢走回车，用户永远发不出带参数命令。
  const slashActive = input.startsWith("/") && !input.trim().includes(" ") && !busy && !showSessionSwitcher;

  // ===== 流式增量节流 =====
  //
  // 问题背景：DeepSeek 等模型一秒能发几十条 chunk，每次 setItems → React
  // 全树重渲 → Ink 写 ANSI 到终端。长时间 streaming 时（如 15s+ 输出），高频
  // 终端写入造成两个问题：
  //
  //   1. 界面抖动：assistant 文本增长导致下面所有元素（工具卡片、状态栏）持续
  //      向下位移，Ink 每秒移动 16 次光标 → 画面不稳定。
  //   2. 滚轮失灵：鼠标向上滚动浏览历史时，Ink 的 ANSI 输出干扰终端 scrollback
  //      buffer → 终端跳回顶部。
  //
  // 策略：自适应节流。同一个 busy 周期内 flush 次数越多 → 间隔越长。
  //   前 5 次：60ms（首次响应快）
  //   5-30 次：60→200ms 线性递增
  //   30 次后：200ms 稳定（约 6s 后达到，此时输出趋于稳定，抖动/滚轮问题消失）
  //
  // 边界事件（tool_start/done/error/cancelled）走 flushNow，不受限。

  const FLUSH_CHAR_THRESHOLD = 200;
  const FLUSH_MAX_MS = 500;

  /** 自适应 flush 间隔：flush 次数越多 → 间隔越长（60→200ms） */
  function adaptiveDelay(): number {
    const n = _flushCount.current;
    if (n <= 5) return 60;
    if (n <= 30) return 60 + Math.round(((n - 5) / 25) * 140);  // 60→200 线性
    return 200;
  }

  const _flushCount = useRef(0);
  const _pendingDelta = useRef<{ reasoning: string; text: string; firstAt: number }>({ reasoning: "", text: "", firstAt: 0 });
  const _flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // prompt.submit 是 fire-and-forget RPC，但后端仍会立刻回一个 accepted/error。
  // 附件队列等这个 ack 后再清空，避免 submit 本身被拒绝时用户需要重新挑文件。
  const _pendingSubmitId = useRef<string | null>(null);
  // MCP 后台加载会按 server 状态变化发事件。这里只记录上一次展示文本，
  // 防止同一快照重复追加 system 行，保持对话流安静。
  const _lastMcpStatusText = useRef("");

  const flushDelta = useCallback(() => {
    _flushTimer.current = null;
    const r = _pendingDelta.current.reasoning;
    const t = _pendingDelta.current.text;
    _pendingDelta.current = { reasoning: "", text: "", firstAt: 0 };
    if (!r && !t) return;
    _flushCount.current += 1;
    setItems((prev) => {
      let next = prev;
      if (r) {
        // 思考内容：创建新的不可变 chunk，永不修改。
        // 旧 chunk 保持原样 → React.memo 让 Ink 跳过重绘。
        next = [...next, { id: nextId(), role: "thought", text: r }];
      }
      if (t) {
        // 助手文本：保持追加行为。流式阶段跳过 Markdown 解析，
        // 只做纯文本追加，render 开销已大幅降低。
        const last = next[next.length - 1];
        if (last && last.role === "assistant") {
          next = [...next.slice(0, -1), { ...last, text: last.text + t }];
        } else {
          next = [...next, { id: nextId(), role: "assistant", text: t }];
        }
      }
      return next;
    });
  }, []);

  const scheduleFlush = useCallback(() => {
    if (_flushTimer.current !== null) return;
    const pending = _pendingDelta.current;
    if (!pending.firstAt) pending.firstAt = Date.now();

    const len = pending.reasoning.length + pending.text.length;
    const elapsed = Date.now() - pending.firstAt;

    // 内容够多 → 立即 flush；超时 → 强制 flush；否则按自适应延迟
    let delay: number;
    if (len >= FLUSH_CHAR_THRESHOLD) {
      delay = 0;
    } else if (elapsed >= FLUSH_MAX_MS) {
      delay = 0;
    } else {
      delay = adaptiveDelay();
    }

    if (delay === 0) {
      flushDelta();
    } else {
      _flushTimer.current = setTimeout(flushDelta, delay);
    }
  }, [flushDelta]);

  // 在边界事件（tool_start / done / round_end / ask_user_question 等）前要立即 flush，
  // 否则攒着的 reasoning 文本会被排到工具卡片后面，时序错乱
  const flushNow = useCallback(() => {
    if (_flushTimer.current !== null) {
      clearTimeout(_flushTimer.current);
    }
    flushDelta();
  }, [flushDelta]);

  /** 重置节流计数器：每个新 busy 周期从 60ms 开始 */
  const resetFlushRhythm = useCallback(() => {
    _flushCount.current = 0;
  }, []);

  const appendSystem = useCallback((text: string) => {
    setItems((prev) => [...prev, { id: nextId(), role: "system", text }]);
  }, []);

  const resetContextWindow = useCallback(() => {
    setContextWindow((prev) => ({
      used_tokens: 0,
      max_tokens: prev?.max_tokens ?? 8000,
      remaining_tokens: prev?.max_tokens ?? 8000,
      percent: 0,
      source: "estimate",
      scope: prev?.scope ?? "state+history",
    }));
  }, []);

  const applySessionPayload = useCallback((payload: SessionPayload, notice?: string) => {
    flushNow();
    resetFlushRhythm();
    setCurrentSession(payload.session ?? null);
    if (payload.context_window !== undefined) {
      setContextWindow(payload.context_window ?? null);
    }
    setItems(() => {
      const restored = restoredHistoryToItems(payload.history ?? []);
      if (notice) restored.push({ id: nextId(), role: "system", text: notice });
      return restored;
    });
    setRound(0);
    setBusy(false);
    setActiveQuestionId(null);
  }, [flushNow, resetFlushRhythm]);

  const refreshSessions = useCallback(async () => {
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const result = await transport.listSessions();
      setSessions(result.sessions ?? []);
      if (result.current !== undefined) setCurrentSession(result.current ?? null);
    } catch (e) {
      setSessionsError((e as Error).message);
    } finally {
      setSessionsLoading(false);
    }
  }, [transport]);

  const openSessionSwitcher = useCallback(() => {
    if (busy) {
      appendSystem("agent 正在工作，等本轮结束后再切换会话。");
      return;
    }
    setInput("");
    setShowSessionSwitcher(true);
    void refreshSessions();
  }, [busy, appendSystem, refreshSessions]);

  const handleSwitchSession = useCallback(async (sessionId: string) => {
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const payload = await transport.switchSession(sessionId);
      applySessionPayload(payload, `已切换到会话 ${payload.session?.session_id ?? sessionId}`);
      setShowSessionSwitcher(false);
      void refreshSessions();
    } catch (e) {
      setSessionsError((e as Error).message);
    } finally {
      setSessionsLoading(false);
    }
  }, [transport, applySessionPayload, refreshSessions]);

  const handleCreateSession = useCallback(async () => {
    setSessionsLoading(true);
    setSessionsError(null);
    try {
      const payload = await transport.createSession();
      applySessionPayload(payload, `已新建并切换到会话 ${payload.session?.session_id ?? "unknown"}`);
      setShowSessionSwitcher(false);
      void refreshSessions();
    } catch (e) {
      setSessionsError((e as Error).message);
    } finally {
      setSessionsLoading(false);
    }
  }, [transport, applySessionPayload, refreshSessions]);

  // 事件订阅
  useEffect(() => {
    const onEvent = (ev: AgentEvent) => {
      switch (ev.type) {
        case "gateway_ready":
          resetFlushRhythm();
          setModel((ev as any).model ?? "unknown");
          setCurrentSession((ev as any).session ?? null);
          if ((ev as any).context_window !== undefined) {
            setContextWindow((ev as any).context_window ?? null);
          }
          if (Array.isArray((ev as any).history) && (ev as any).history.length > 0) {
            setItems(restoredHistoryToItems((ev as any).history));
          }
          break;

        case "mcp_status": {
          const text = formatMCPStatus(ev as any);
          if (text && text !== _lastMcpStatusText.current) {
            _lastMcpStatusText.current = text;
            appendSystem(text);
          }
          break;
        }

        case "pet_updated": {
          setPetState((ev as any).state ?? null);
          break;
        }

        case "round_start":
          setRound((ev as any).round_idx);
          setMaxRounds((ev as any).max_rounds);
          break;

        case "text_delta": {
          const delta = (ev as any).delta as string;
          _pendingDelta.current.text += delta;
          scheduleFlush();
          break;
        }

        case "reasoning_delta": {
          // 思考流：增量累积，flush 时创建独立的不可变 chunk
          const delta = (ev as any).delta as string;
          _pendingDelta.current.reasoning += delta;
          scheduleFlush();
          break;
        }

        case "todo_list_updated": {
          // 按用户偏好：每次写入都新增一张卡片，不去重 / 替换
          flushNow();
          const e = ev as any;
          setItems((prev) => [...prev, {
            id: nextId(),
            role: "todo",
            text: "",
            todoItems: e.items ?? [],
          }]);
          break;
        }

        case "tool_start": {
          flushNow();
          const e = ev as any;
          setItems((prev) => [...prev, {
            id: nextId(),
            role: "tool",
            text: "",
            toolName: e.name,
            toolArgs: e.arguments,
            toolDone: false,
            collapsed: false,
          }]);
          break;
        }

        case "tool_complete": {
          flushNow();
          const e = ev as any;
          setItems((prev) => {
            // 从后往前找最近一个未完成的同 name 工具（cb-agent call_id 不在 ToolStart 上裸传，
            // 但同名 + 顺序匹配在并发场景下也基本可靠；假如要更严，按 call_id 过滤）
            for (let i = prev.length - 1; i >= 0; i--) {
              const it = prev[i];
              if (it.role === "tool" && !it.toolDone && it.toolName === e.name) {
                const updated: ChatItem = {
                  ...it,
                  toolResult: e.result,
                  toolDuration: e.duration_seconds,
                  toolError: e.is_error,
                  toolDone: true,
                };
                return [...prev.slice(0, i), updated, ...prev.slice(i + 1)];
              }
            }
            return prev;
          });
          break;
        }

        case "token_usage": {
          const e = ev as any;
          setPromptTokens((p) => p + e.prompt_tokens);
          setCompletionTokens((c) => c + e.completion_tokens);
          break;
        }

        case "done":
          flushNow();
          resetFlushRhythm();
          if ((ev as any).context_window !== undefined) {
            setContextWindow((ev as any).context_window ?? null);
          }
          {
            const notice = describeAutoCompact(ev);
            if (notice) appendSystem(notice);
          }
          setBusy(false);
          setRound(0);
          break;

        case "error":
          flushNow();
          resetFlushRhythm();
          appendSystem(`✗ ${(ev as any).where}: ${(ev as any).message}`);
          setBusy(false);
          break;

        case "cancelled":
          flushNow();
          resetFlushRhythm();
          appendSystem(`⏸ 已中断 (${(ev as any).where})`);
          setBusy(false);
          break;

        case "ask_user_question": {
          flushNow();
          const e = ev as any;
          setItems((prev) => [...prev, {
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
          }]);
          setActiveQuestionId(e.question_id);
          break;
        }

        case "ask_user_question_answered": {
          flushNow();
          const e = ev as any;
          setItems((prev) => {
            for (let i = prev.length - 1; i >= 0; i--) {
              const it = prev[i];
              if (it.role === "ask_question" && it.questionId === e.question_id) {
                const updated: ChatItem = {
                  ...it,
                  answered: true,
                  answerLabels: e.selected_labels ?? [],
                  answerOther: e.other_text ?? undefined,
                  answerCancelled: !!e.cancelled,
                };
                return [...prev.slice(0, i), updated, ...prev.slice(i + 1)];
              }
            }
            return prev;
          });
          setActiveQuestionId((curr) => (curr === e.question_id ? null : curr));
          break;
        }

        default:
          break;
      }
    };

    const onProtoErr = (raw: string, err: Error) => {
      setProtocolErrors((n) => n + 1);
      appendSystem(`协议解析错误：${err.message}（详情见 ${transport.stderrLogFile}）`);
    };

    const onExit = (code: number | null) => {
      appendSystem(`Python agent 进程退出 (code=${code ?? "?"})`);
      setTimeout(() => exit(), 300);
    };

    const onStderr = (line: string) => {
      setStderrLines((prev) => {
        const next = prev.length >= STDERR_RING_MAX ? prev.slice(-STDERR_RING_MAX + 1) : prev.slice();
        next.push(line);
        return next;
      });
    };

    const onResponse = (id: string | number, body: { result?: unknown; error?: { code: number; message: string } }) => {
      if (id !== _pendingSubmitId.current) return;
      _pendingSubmitId.current = null;
      if (body.error) {
        setBusy(false);
        appendSystem(`提交失败：${body.error.message}`);
        return;
      }
      setAttachments([]);
    };

    transport.on("event", onEvent);
    transport.on("response", onResponse);
    transport.on("protocolError", onProtoErr);
    transport.on("exit", onExit);
    transport.on("stderr", onStderr);
    return () => {
      transport.removeListener("event", onEvent);
      transport.removeListener("response", onResponse);
      transport.removeListener("protocolError", onProtoErr);
      transport.removeListener("exit", onExit);
      transport.removeListener("stderr", onStderr);
      if (_flushTimer.current !== null) {
        clearTimeout(_flushTimer.current);
        _flushTimer.current = null;
      }
    };
  }, [transport, exit, appendSystem, flushNow, scheduleFlush, resetFlushRhythm]);

  useEffect(() => {
    let disposed = false;
    transport.getPetState()
      .then((state) => {
        if (!disposed) setPetState(state ?? null);
      })
      .catch(() => {
        if (!disposed) setPetState(null);
      });
    return () => {
      disposed = true;
    };
  }, [transport]);

  // 键盘事件：Ctrl-C 在 busy 时中断/空闲时退出；Ctrl-O 切换后端日志面板
  useInput((inputChar, key) => {
    if (key.ctrl && inputChar === "c") {
      if (busy) {
        transport.cancel();
      } else {
        transport.quit();
        setTimeout(() => exit(), 200);
      }
    } else if (key.ctrl && inputChar === "o") {
      setShowActivity((v) => !v);
    } else if (key.ctrl && inputChar === "e") {
      // Ctrl+E：展开/折叠历史消息。busy 期间强制折叠防抖动。
      setShowAllMessages((v) => !v);
    } else if (key.ctrl && inputChar === "l") {
      // 仿 bash：清当前屏幕显示，但保留 React items 和后端 history
      // 不清 items 的话，scrollback 会重新长出来；这里直接清 items 拿到"干净屏"
      // 想保留前端可视历史的话以后可以拆成 Ctrl+L=只清屏 / /clear=连后端一起清
      setItems([]);
      clearScreen?.();
    } else if (key.ctrl && (inputChar === "v" || inputChar === "\u0016")) {
      if (busy || activeQuestionId !== null || showSessionSwitcher) return;
      readClipboardImageAttachment()
        .then((item) => {
          setAttachments((prev) => [...prev, item]);
          appendSystem(`已从剪贴板添加图片：${item.fileName}`);
        })
        .catch((e) => appendSystem(`剪贴板图片读取失败：${(e as Error).message}`));
    }
  });

  /** 命令面板里选中或输入框里完整输入命令时调用 */
  const runCommand = useCallback((cmd: SlashCommand, commandLine = input) => {
    const normalized = commandLine.trim() || cmd.name;
    const ctx: CommandCtx = {
      transport,
      input: normalized,
      args: normalized.slice(cmd.name.length).trim(),
      appendSystem: (t) => appendSystem(t),
      setItems,
      applySessionPayload,
      setContextWindow,
      resetContextWindow,
      openSessionSwitcher,
      toggleActivity: () => setShowActivity((v) => !v),
      setPetState,
      attachments,
      setAttachments,
    };
    const ret = cmd.handler(ctx);
    if (ret instanceof Promise) {
      ret.catch((e) => appendSystem(`✗ 命令 ${cmd.name} 抛错：${(e as Error).message}`));
    }
    setInput("");
  }, [transport, input, appendSystem, applySessionPayload, setContextWindow, resetContextWindow, openSessionSwitcher, attachments]);

  const handleSubmit = useCallback((text: string) => {
    const pendingAttachments = attachments;
    if ((!text.trim() && pendingAttachments.length === 0) || busy) return;

    // 斜杠命令：拦截，不走 prompt.submit，也不入历史
    if (text.startsWith("/")) {
      const cmd = findCommand(text);
      if (cmd) {
        runCommand(cmd, text);
      } else {
        appendSystem(`未知命令：${text.split(/\s+/)[0]}（输入 / 查看可用命令）`);
      }
      setInput("");
      return;
    }

    if (text.trim()) historyStore.push(text);
    const attachmentLines = pendingAttachments.map((item, index) => {
      return `  ${index + 1}. ${item.fileName} (${item.source ?? "direct"})`;
    });
    const displayText = [
      text.trim() || "请根据附件回答。",
      attachmentLines.length ? "附件：\n" + attachmentLines.join("\n") : "",
    ].filter(Boolean).join("\n\n");
    const submitAttachments: PromptAttachmentInput[] = pendingAttachments.map(({ path, modality, source }) => ({
      path,
      modality,
      source,
    }));

    setItems((prev) => [...prev, { id: nextId(), role: "user", text: displayText }]);
    setInput("");
    setBusy(true);
    _pendingSubmitId.current = transport.sendPrompt(text, submitAttachments);
  }, [attachments, busy, transport, appendSystem, runCommand]);

  /** ↑/↓ 翻历史的回调：idx 0 = 最新一条，递增 = 更老。null 表示越界 */
  const getHistoryAt = useCallback((idx: number): string | null => {
    const all = historyStore.all();
    if (idx < 0 || idx >= all.length) return null;
    return all[all.length - 1 - idx];
  }, []);

  const handleAnswerQuestion = useCallback(
    (questionId: string, params: { selected_labels: string[]; other_text?: string; cancelled?: boolean }) => {
      transport.answerQuestion({ question_id: questionId, ...params });
      // 不立刻清 activeQuestionId：等 ask_user_question_answered 事件回来再清，
      // 避免重复发或在网络/进程慢时面板提前消失
    },
    [transport],
  );

  return (
    <Box flexDirection="column" padding={1}>
      <Banner model={model} cwd={process.cwd()} />

      <EventStream
        items={items}
        busy={busy}
        showAll={showAllMessages}
        onAnswerQuestion={handleAnswerQuestion}
        activeQuestionId={activeQuestionId}
      />

      <Box marginTop={1}>
        <ActivityPanel
          lines={stderrLines}
          visible={showActivity}
          logFile={transport.stderrLogFile}
        />
      </Box>

      <Box marginTop={1} flexDirection="column">
        {showSessionSwitcher && (
          <SessionSwitcher
            sessions={sessions}
            currentSessionId={currentSession?.session_id}
            loading={sessionsLoading}
            error={sessionsError}
            onSwitch={handleSwitchSession}
            onNew={handleCreateSession}
            onRefresh={refreshSessions}
            onCancel={() => setShowSessionSwitcher(false)}
          />
        )}
        {slashActive && (
          <SlashCommandPicker
            query={input.slice(1)}
            onSelect={(cmd) => runCommand(cmd, cmd.name)}
            onCancel={() => setInput("")}
          />
        )}
        <AttachmentQueue attachments={attachments} />
        <Box flexDirection="row" alignItems="flex-end">
          <Box flexGrow={1} flexShrink={1}>
            <PromptInput
              value={input}
              onChange={setInput}
              onSubmit={handleSubmit}
              disabled={busy || activeQuestionId !== null || showSessionSwitcher}
              getHistoryAt={getHistoryAt}
              delegateNavKeys={slashActive || activeQuestionId !== null || showSessionSwitcher}
            />
          </Box>
        </Box>
        <Box marginTop={1}>
          <StatusBar
            model={model}
            sessionId={currentSession?.session_id}
            promptTokens={promptTokens}
            completionTokens={completionTokens}
            contextWindow={contextWindow}
            round={round}
            maxRounds={maxRounds}
            busy={busy}
          />
        </Box>
      </Box>
    </Box>
  );
}
