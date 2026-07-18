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
import { getLayoutMode, shortSessionId } from "../layout.js";
import { textAttributes } from "../theme.js";

function formatPercent(percent: number): string {
  if (!Number.isFinite(percent) || percent <= 0) return "0%";
  if (percent < 10) return `${percent.toFixed(1)}%`;
  return `${Math.round(percent)}%`;
}

export function Footer() {
  const theme = useTheme();
  const { state } = useSession();
  const dimensions = useTerminalDimensions();

  const ctxPercent = () => state.contextWindow?.percent ?? 0;
  const layoutMode = () => getLayoutMode(dimensions().width);
  const contextLeft = () => Math.max(0, Math.min(100, 100 - ctxPercent()));
  const ctxColor = createMemo(() => {
    const left = contextLeft();
    return left <= 10 ? theme.error : left <= 35 ? theme.warning : theme.text;
  });
  const mcpConnected = () => state.mcp?.connected ?? 0;
  const mcpFailed = () => state.mcp?.failed ?? 0;
  const mode = () => state.planState?.mode ?? "execute";
  const planStatus = () => state.planState?.status ?? "idle";
  const permissionLabel = () => state.permissionMode === "full_access" ? "FULL" : "ASK";
  const contextLabel = () => state.contextWindow
    ? `${formatPercent(contextLeft())} context left`
    : "--% context left";
  const busyLabel = () => state.pending || (state.busy ? "working" : null);

  return (
    <box flexDirection="row" justifyContent="space-between" flexShrink={0} minWidth={0}>
      <box flexDirection="row" flexShrink={1} minWidth={0}>
        <Show when={busyLabel()}>
          <Spinner color={theme.info} />
          <text fg={theme.text}>{` ${busyLabel()}  `}</text>
        </Show>
        <Show when={!busyLabel()}>
          <text fg={theme.text} attributes={textAttributes.muted} wrapMode="none" truncate>
            <Show when={layoutMode() !== "narrow"}>{state.model}{"  "}</Show>
            <span style={{ fg: mode() === "plan" ? theme.agent : theme.text }}>
              {mode() === "plan" ? "PLAN" : "EXEC"}
              {mode() === "plan" && planStatus() !== "idle" ? `:${planStatus()}` : ""}
            </span>
            <span
              style={{
                fg: state.permissionMode === "full_access" ? theme.error : theme.text,
                attributes: state.permissionMode === "full_access" ? undefined : textAttributes.muted,
              }}
            >
              {`  ${permissionLabel()}`}
            </span>
            <Show when={layoutMode() === "wide" && state.session?.session_id}>
              {`  ${shortSessionId(state.session!.session_id)}`}
            </Show>
          </text>
        </Show>
      </box>

      <text fg={theme.text} attributes={textAttributes.muted} wrapMode="none" flexShrink={0}>
        <span style={{ fg: ctxColor() }}>{contextLabel()}</span>
        <Show when={layoutMode() === "wide"}>
          {`  In ${formatContextTokenCount(state.promptTokens, state.contextWindow?.source)}`}
          {`  Out ${formatTokenCount(state.completionTokens)}`}
        </Show>
        <Show when={layoutMode() !== "narrow" && (mcpConnected() > 0 || mcpFailed() > 0)}>
          <span style={{ fg: mcpFailed() > 0 ? theme.error : theme.success }}>
            {`  ${mcpFailed() > 0 ? "!" : "•"} `}
          </span>
          {mcpConnected()} MCP
        </Show>
      </text>
    </box>
  );
}
