/**
 * Sidebar：右侧信息栏（M6）。
 *
 * 复刻 opencode sidebar 的结构（标题区 + 内容区 + 底部品牌行），数据换成 cb-agent：
 *   - 会话标题 / session id
 *   - 当前模型
 *   - MCP 服务器列表与状态
 *   - 底部 cb-agent 品牌行
 *
 * 固定宽度 42（与 opencode 一致），不参与横向 flex 伸缩。
 */

import { For, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";

const SIDEBAR_WIDTH = 42;

export function Sidebar() {
  const theme = useTheme();
  const { state } = useSession();

  const servers = () => state.mcp?.servers ?? [];

  return (
    <box
      width={SIDEBAR_WIDTH}
      flexShrink={0}
      flexDirection="column"
      borderColor={theme.border}
      border={["left"]}
      paddingLeft={2}
      paddingRight={1}
    >
      {/* 标题区 */}
      <box flexDirection="column">
        <text fg={theme.text}>
          <b>{state.session?.active_task || state.session?.session_id || "新会话"}</b>
        </text>
        <Show when={state.session?.session_id}>
          <text fg={theme.textMuted}>{state.session!.session_id}</text>
        </Show>
      </box>

      {/* 模型 */}
      <box flexDirection="column" paddingTop={1}>
        <text fg={theme.textMuted}>模型</text>
        <text fg={theme.text}>{state.model}</text>
      </box>

      {/* MCP 服务器 */}
      <Show when={servers().length > 0}>
        <box flexDirection="column" paddingTop={1}>
          <text fg={theme.textMuted}>MCP</text>
          <For each={servers()}>
            {(s) => {
              const color =
                s.status === "connected"
                  ? theme.success
                  : s.status === "error"
                    ? theme.error
                    : theme.textMuted;
              return (
                <text fg={theme.text}>
                  <span style={{ fg: color }}>⊙ </span>
                  {s.name}
                  <span style={{ fg: theme.textMuted }}>
                    {s.tools_count ? `  ${s.tools_count} tools` : ""}
                  </span>
                </text>
              );
            }}
          </For>
        </box>
      </Show>

      {/* 占位填充，把品牌行推到底部 */}
      <box flexGrow={1} />

      {/* 底部品牌行 */}
      <box flexShrink={0} paddingTop={1}>
        <text fg={theme.textMuted}>
          <span style={{ fg: theme.success }}>•</span> <b>cb</b>
          <span style={{ fg: theme.text }}>
            <b>-agent</b>
          </span>{" "}
          OTUI
        </text>
      </box>
    </box>
  );
}
