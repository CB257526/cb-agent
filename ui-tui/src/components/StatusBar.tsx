import React from "react";
import { Box, Text } from "ink";
import { Spinner } from "./Spinner.js";
import { Byline } from "./Byline.js";
import { KeyboardShortcutHint } from "./KeyboardShortcutHint.js";
import { theme } from "../theme.js";
import type { ContextWindow } from "../types.js";

export interface StatusBarProps {
  model: string;
  sessionId?: string | null;
  promptTokens: number;
  completionTokens: number;
  contextWindow?: ContextWindow | null;
  round: number;
  maxRounds: number;
  busy: boolean;
}

/** 底部状态栏：左边 spinner + 模型/上下文窗口/累计用量/round；右边快捷键 byline。 */
export function StatusBar({ model, sessionId, promptTokens, completionTokens, contextWindow, round, maxRounds, busy }: StatusBarProps) {
  const totalK = ((promptTokens + completionTokens) / 1000).toFixed(1);
  const contextUsed = contextWindow?.used_tokens ?? 0;
  const contextMax = contextWindow?.max_tokens ?? 8000;
  const contextPercent = contextWindow?.percent ?? 0;
  const contextColor = contextPercent >= 85 ? theme.error : contextPercent >= 65 ? theme.warning : theme.success;
  return (
    <Box flexDirection="row" justifyContent="space-between">
      <Box>
        {busy && <Text color={theme.warning}><Spinner color={theme.warning} /> </Text>}
        <Text dimColor>
          <Byline>
            <Text>{model}</Text>
            {sessionId ? <Text>session {shortSessionId(sessionId)}</Text> : null}
            <Text>
              Context {formatTokenCount(contextUsed)}/{formatTokenCount(contextMax)}{" "}
              <Text color={contextColor}>{formatPercent(contextPercent)}</Text>
            </Text>
            <Text>usage {totalK}k</Text>
            {round > 0 ? <Text>round {round}/{maxRounds}</Text> : null}
            {busy ? <Text color={theme.warning}>working…</Text> : null}
          </Byline>
        </Text>
      </Box>
      <Box>
        <Text dimColor>
          <Byline>
            <KeyboardShortcutHint shortcut="Enter" action="send" />
            <KeyboardShortcutHint shortcut="↑/↓" action="history" />
            <KeyboardShortcutHint shortcut="/" action="commands" />
            <KeyboardShortcutHint shortcut="/sessions" action="switch" />
            <KeyboardShortcutHint shortcut="Ctrl-O" action="log" />
            <KeyboardShortcutHint shortcut="Ctrl-L" action="clear" />
            <KeyboardShortcutHint shortcut="Ctrl-C" action={busy ? "cancel" : "exit"} />
          </Byline>
        </Text>
      </Box>
    </Box>
  );
}

function shortSessionId(sessionId: string): string {
  const parts = sessionId.split("_");
  if (parts.length >= 4) return `${parts[1]}_${parts[2]}_${parts[3]}`;
  return sessionId;
}

function formatTokenCount(tokens: number): string {
  if (!Number.isFinite(tokens) || tokens <= 0) return "0";
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}k`;
  return `${Math.round(tokens)}`;
}

function formatPercent(percent: number): string {
  if (!Number.isFinite(percent) || percent <= 0) return "0%";
  if (percent < 10) return `${percent.toFixed(1)}%`;
  return `${Math.round(percent)}%`;
}
