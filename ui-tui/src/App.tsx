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
import { AgentEvent, ChatItem, RestoredHistoryMessage, SessionPayload, SessionSummary } from "./types.js";
import { EventStream } from "./components/EventStream.js";
import { StatusBar } from "./components/StatusBar.js";
import { PromptInput } from "./components/PromptInput.js";
import { ActivityPanel } from "./components/ActivityPanel.js";
import { Banner } from "./components/Banner.js";
import { SlashCommandPicker } from "./components/SlashCommandPicker.js";
import { SessionSwitcher } from "./components/SessionSwitcher.js";
import { HistoryStore } from "./historyStore.js";
import { findCommand, SlashCommand, CommandCtx } from "./commands.js";

const STDERR_RING_MAX = 200;  // 内存里最多留 200 行，超出从头丢

// 单例：历史只在进程内加载一次
const historyStore = new HistoryStore();
historyStore.load();

let _idCounter = 0;
const nextId = () => `i${++_idCounter}`;

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
  const [protocolErrors, setProtocolErrors] = useState(0);
  const [stderrLines, setStderrLines] = useState<string[]>([]);
  const [showActivity, setShowActivity] = useState(false);
  const [currentSession, setCurrentSession] = useState<SessionSummary | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [showSessionSwitcher, setShowSessionSwitcher] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState<string | null>(null);
  // 当前等待用户作答的问题 id：决定 EventStream 把输入路由给哪个 panel；
  // 同时 PromptInput 在问答期 disabled，避免误打字提交 prompt
  const [activeQuestionId, setActiveQuestionId] = useState<string | null>(null);

  // / 命令面板：只在输入"纯命令名前缀"时显示。带参数的命令（如
  // /switch <session_id>）需要让 Enter 直接提交给 handleSubmit，否则 picker
  // 会抢走回车，用户永远发不出带参数命令。
  const slashActive = input.startsWith("/") && !input.trim().includes(" ") && !busy && !showSessionSwitcher;

  // 流式增量节流：DeepSeek thinking/text 一秒能发几十条 chunk，每条都 setItems
  // 会导致 ink 整树重渲 + stdout ANSI 全屏重画 → 事件循环压不过来 → stdin pipe
  // 反压回 Python，整条链路就卡住。把高频 delta 累积到 ref，每 ~60ms flush 一次。
  const _pendingDelta = useRef<{ reasoning: string; text: string }>({ reasoning: "", text: "" });
  const _flushTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flushDelta = useCallback(() => {
    _flushTimer.current = null;
    const r = _pendingDelta.current.reasoning;
    const t = _pendingDelta.current.text;
    _pendingDelta.current = { reasoning: "", text: "" };
    if (!r && !t) return;
    setItems((prev) => {
      let next = prev;
      if (r) {
        const last = next[next.length - 1];
        if (last && last.role === "thought") {
          next = [...next.slice(0, -1), { ...last, text: last.text + r }];
        } else {
          next = [...next, { id: nextId(), role: "thought", text: r }];
        }
      }
      if (t) {
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
    _flushTimer.current = setTimeout(flushDelta, 60);
  }, [flushDelta]);

  // 在边界事件（tool_start / done / round_end / ask_user_question 等）前要立即 flush，
  // 否则攒着的 reasoning 文本会被排到工具卡片后面，时序错乱
  const flushNow = useCallback(() => {
    if (_flushTimer.current !== null) {
      clearTimeout(_flushTimer.current);
    }
    flushDelta();
  }, [flushDelta]);

  const appendSystem = useCallback((text: string) => {
    setItems((prev) => [...prev, { id: nextId(), role: "system", text }]);
  }, []);

  const applySessionPayload = useCallback((payload: SessionPayload, notice?: string) => {
    flushNow();
    setCurrentSession(payload.session ?? null);
    setItems(() => {
      const restored = restoredHistoryToItems(payload.history ?? []);
      if (notice) restored.push({ id: nextId(), role: "system", text: notice });
      return restored;
    });
    setRound(0);
    setBusy(false);
    setActiveQuestionId(null);
  }, [flushNow]);

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
          setModel((ev as any).model ?? "unknown");
          setCurrentSession((ev as any).session ?? null);
          if (Array.isArray((ev as any).history) && (ev as any).history.length > 0) {
            setItems(restoredHistoryToItems((ev as any).history));
          }
          break;

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
          // 思考流：增量拼接到最近一个 thought item；遇到非 thought（如 assistant
          // 文本已经开始 / 工具块插进来）就开新的一块，让 thought 始终独立成段
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
          setBusy(false);
          setRound(0);
          break;

        case "error":
          flushNow();
          appendSystem(`✗ ${(ev as any).where}: ${(ev as any).message}`);
          setBusy(false);
          break;

        case "cancelled":
          flushNow();
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

    transport.on("event", onEvent);
    transport.on("protocolError", onProtoErr);
    transport.on("exit", onExit);
    transport.on("stderr", onStderr);
    return () => {
      transport.removeListener("event", onEvent);
      transport.removeListener("protocolError", onProtoErr);
      transport.removeListener("exit", onExit);
      transport.removeListener("stderr", onStderr);
      if (_flushTimer.current !== null) {
        clearTimeout(_flushTimer.current);
        _flushTimer.current = null;
      }
    };
  }, [transport, exit, appendSystem, flushNow, scheduleFlush]);

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
    } else if (key.ctrl && inputChar === "l") {
      // 仿 bash：清当前屏幕显示，但保留 React items 和后端 history
      // 不清 items 的话，scrollback 会重新长出来；这里直接清 items 拿到"干净屏"
      // 想保留前端可视历史的话以后可以拆成 Ctrl+L=只清屏 / /clear=连后端一起清
      setItems([]);
      clearScreen?.();
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
      openSessionSwitcher,
      toggleActivity: () => setShowActivity((v) => !v),
    };
    const ret = cmd.handler(ctx);
    if (ret instanceof Promise) {
      ret.catch((e) => appendSystem(`✗ 命令 ${cmd.name} 抛错：${(e as Error).message}`));
    }
    setInput("");
  }, [transport, input, appendSystem, applySessionPayload, openSessionSwitcher]);

  const handleSubmit = useCallback((text: string) => {
    if (!text.trim() || busy) return;

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

    historyStore.push(text);
    setItems((prev) => [...prev, { id: nextId(), role: "user", text }]);
    setInput("");
    setBusy(true);
    transport.sendPrompt(text);
  }, [busy, transport, appendSystem, runCommand]);

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
        <PromptInput
          value={input}
          onChange={setInput}
          onSubmit={handleSubmit}
          disabled={busy || activeQuestionId !== null || showSessionSwitcher}
          getHistoryAt={getHistoryAt}
          delegateNavKeys={slashActive || activeQuestionId !== null || showSessionSwitcher}
        />
        <Box marginTop={1}>
          <StatusBar
            model={model}
            sessionId={currentSession?.session_id}
            promptTokens={promptTokens}
            completionTokens={completionTokens}
            round={round}
            maxRounds={maxRounds}
            busy={busy}
          />
        </Box>
      </Box>
    </Box>
  );
}
