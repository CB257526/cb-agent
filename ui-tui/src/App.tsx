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
import { AgentEvent, ChatItem } from "./types.js";
import { EventStream } from "./components/EventStream.js";
import { StatusBar } from "./components/StatusBar.js";
import { PromptInput } from "./components/PromptInput.js";
import { ActivityPanel } from "./components/ActivityPanel.js";
import { Banner } from "./components/Banner.js";
import { SlashCommandPicker } from "./components/SlashCommandPicker.js";
import { HistoryStore } from "./historyStore.js";
import { findCommand, SlashCommand, CommandCtx } from "./commands.js";

const STDERR_RING_MAX = 200;  // 内存里最多留 200 行，超出从头丢

// 单例：历史只在进程内加载一次
const historyStore = new HistoryStore();
historyStore.load();

let _idCounter = 0;
const nextId = () => `i${++_idCounter}`;

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

  // / 命令面板：input 以 '/' 开头时自动显示，picker 自己读 input.slice(1) 作 query
  const slashActive = input.startsWith("/") && !busy;

  // useRef 给事件 handler 用，否则闭包里拿到的是旧 setItems
  const itemsRef = useRef(items);
  itemsRef.current = items;

  const appendSystem = useCallback((text: string, color: "red" | "yellow" | "gray" = "gray") => {
    setItems((prev) => [...prev, { id: nextId(), role: "system", text }]);
  }, []);

  // 事件订阅
  useEffect(() => {
    const onEvent = (ev: AgentEvent) => {
      switch (ev.type) {
        case "gateway_ready":
          setModel((ev as any).model ?? "unknown");
          break;

        case "round_start":
          setRound((ev as any).round_idx);
          setMaxRounds((ev as any).max_rounds);
          break;

        case "text_delta": {
          const delta = (ev as any).delta as string;
          setItems((prev) => {
            const last = prev[prev.length - 1];
            if (last && last.role === "assistant") {
              const updated = { ...last, text: last.text + delta };
              return [...prev.slice(0, -1), updated];
            }
            return [...prev, { id: nextId(), role: "assistant", text: delta }];
          });
          break;
        }

        case "tool_start": {
          const e = ev as any;
          setItems((prev) => [...prev, {
            id: nextId(),
            role: "tool",
            text: "",
            toolName: e.name,
            toolArgs: e.arguments,
            toolDone: false,
            collapsed: true,
          }]);
          break;
        }

        case "tool_complete": {
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
          setBusy(false);
          setRound(0);
          break;

        case "error":
          appendSystem(`✗ ${(ev as any).where}: ${(ev as any).message}`);
          setBusy(false);
          break;

        case "cancelled":
          appendSystem(`⏸ 已中断 (${(ev as any).where})`);
          setBusy(false);
          break;

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
    };
  }, [transport, exit, appendSystem]);

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

  const handleSubmit = useCallback((text: string) => {
    if (!text.trim() || busy) return;

    // 斜杠命令：拦截，不走 prompt.submit，也不入历史
    if (text.startsWith("/")) {
      const cmd = findCommand(text);
      if (cmd) {
        runCommand(cmd);
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
  }, [busy, transport, appendSystem]);

  /** 命令面板里选中或输入框里完整输入命令时调用 */
  const runCommand = useCallback((cmd: SlashCommand) => {
    const ctx: CommandCtx = {
      transport,
      appendSystem: (t) => appendSystem(t),
      setItems,
      toggleActivity: () => setShowActivity((v) => !v),
    };
    const ret = cmd.handler(ctx);
    if (ret instanceof Promise) {
      ret.catch((e) => appendSystem(`✗ 命令 ${cmd.name} 抛错：${(e as Error).message}`));
    }
    setInput("");
  }, [transport, appendSystem]);

  /** ↑/↓ 翻历史的回调：idx 0 = 最新一条，递增 = 更老。null 表示越界 */
  const getHistoryAt = useCallback((idx: number): string | null => {
    const all = historyStore.all();
    if (idx < 0 || idx >= all.length) return null;
    return all[all.length - 1 - idx];
  }, []);

  return (
    <Box flexDirection="column" padding={1}>
      <Banner model={model} cwd={process.cwd()} />

      <EventStream items={items} />

      <Box marginTop={1}>
        <ActivityPanel
          lines={stderrLines}
          visible={showActivity}
          logFile={transport.stderrLogFile}
        />
      </Box>

      <Box marginTop={1} flexDirection="column">
        {slashActive && (
          <SlashCommandPicker
            query={input.slice(1)}
            onSelect={(cmd) => runCommand(cmd)}
            onCancel={() => setInput("")}
          />
        )}
        <PromptInput
          value={input}
          onChange={setInput}
          onSubmit={handleSubmit}
          disabled={busy}
          getHistoryAt={getHistoryAt}
          delegateNavKeys={slashActive}
        />
        <Box marginTop={1}>
          <StatusBar
            model={model}
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
