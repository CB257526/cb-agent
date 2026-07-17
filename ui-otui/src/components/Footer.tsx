/**
 * Footer：底部状态栏（M6）。
 *
 * 复刻 opencode footer 的左右布局：左侧目录，右侧状态指标。但字段换成 cb-agent 已有数据：
 *   model · session · Context 用量%（按阈值着色）· usage 累计 token · round · MCP 计数
 *
 * opencode 的 LSP/permission 概念 cb-agent 没有，省去；MCP 用 store.mcp 填充。
 */

import { createMemo, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import { Spinner } from "./Spinner.js";
import { useTerminalDimensions } from "@opentui/solid";
import { formatContextTokenCount, formatTokenCount } from "../tokenDisplay.js";

function formatPercent(percent: number): string {
  if (!Number.isFinite(percent) || percent <= 0) return "0%";
  if (percent < 10) return `${percent.toFixed(1)}%`;
  return `${Math.round(percent)}%`;
}

function shortSessionId(sessionId: string): string {
  if (typeof sessionId !== "string") return String(sessionId ?? "");
  const parts = sessionId.split("_");
  if (parts.length >= 4) return `${parts[1]}_${parts[2]}_${parts[3]}`;
  return sessionId;
}

export function Footer() {
  const theme = useTheme();
  const { state } = useSession();
  const dimensions = useTerminalDimensions();

  const ctxUsed = () => state.contextWindow?.used_tokens ?? 0;
  const ctxMax = () => state.contextWindow?.full_window_tokens ?? state.contextWindow?.max_tokens ?? 8000;
  const ctxPercent = () => state.contextWindow?.percent ?? 0;
  const compactLayout = () => dimensions().width < 110;
  const ctxColor = createMemo(() => {
    const p = ctxPercent();
    const trigger = state.contextWindow?.auto_compact_trigger_percent ?? 90;
    return p >= trigger ? theme.error : p >= 65 ? theme.warning : theme.success;
  });
  const mcpConnected = () => state.mcp?.connected ?? 0;
  const mcpFailed = () => state.mcp?.failed ?? 0;
  const mode = () => state.planState?.mode ?? "execute";
  const planStatus = () => state.planState?.status ?? "idle";
  const permissionLabel = () => state.permissionMode === "full_access" ? "FULL" : "ASK";

  return (
    <box flexDirection="row" justifyContent="space-between" flexShrink={0} paddingLeft={1} paddingRight={1}>
      <box flexDirection="row" flexShrink={1} minWidth={0}>
        <Show when={state.busy || state.pending}>
          <Spinner color={theme.warning} />
          <text fg={theme.textMuted}>{" "}</text>
        </Show>
        <Show when={state.pending}>
          <text fg={theme.warning}>{state.pending}{"  "}</text>
        </Show>
        <text fg={theme.textMuted}>
          {state.model}
          <span style={{ fg: mode() === "plan" ? theme.warning : theme.success }}>
            {"  "}{mode() === "plan" ? "PLAN" : "EXEC"}
            {mode() === "plan" && planStatus() !== "idle" ? `:${planStatus()}` : ""}
          </span>
          <span style={{ fg: state.permissionMode === "full_access" ? theme.error : theme.textMuted }}>
            {"  "}{permissionLabel()}
          </span>
          <Show when={state.session && !compactLayout()}>
            <span style={{ fg: theme.textMuted }}> · {shortSessionId(state.session!.session_id)}</span>
          </Show>
        </text>
      </box>

      <box flexDirection="row" flexShrink={0}>
        <text fg={theme.textMuted}>
          Context {formatContextTokenCount(ctxUsed(), state.contextWindow?.source)}/{formatTokenCount(ctxMax())}{" "}
          <span style={{ fg: ctxColor() }}>{formatPercent(ctxPercent())}</span>
          {"  "}Input {formatTokenCount(state.promptTokens)}
          <Show when={!compactLayout() && state.cachedPromptTokens > 0}>
            <span style={{ fg: theme.textMuted }}> (Cached {formatTokenCount(state.cachedPromptTokens)})</span>
          </Show>
          {"  "}Output {formatTokenCount(state.completionTokens)}
          <Show when={state.round > 0 && !compactLayout()}>
            <span style={{ fg: theme.textMuted }}>{`  round ${state.round}/${state.maxRounds}`}</span>
          </Show>
        </text>
        <Show when={mcpConnected() > 0 || mcpFailed() > 0}>
          <text fg={theme.text}>
            {"  "}
            <span style={{ fg: mcpFailed() > 0 ? theme.error : theme.success }}>⊙ </span>
            {mcpConnected()} MCP
          </text>
        </Show>
      </box>
    </box>
  );
}
