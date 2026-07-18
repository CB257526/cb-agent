/**
 * 单行会话标题。
 *
 * 固定侧栏移除后，标题只保留识别当前任务所需的信息。模型、权限和上下文指标交给
 * Footer，避免顶部和底部重复展示同一批状态。
 */

import { Show } from "solid-js";
import { useTerminalDimensions } from "@opentui/solid";
import { useSession } from "../context/session.js";
import { useTheme } from "../context/theme.js";
import { getLayoutMode, shortSessionId } from "../layout.js";
import { textAttributes } from "../theme.js";

export function SessionHeader() {
  const theme = useTheme();
  const { state } = useSession();
  const dimensions = useTerminalDimensions();
  const mode = () => getLayoutMode(dimensions().width);
  const title = () => state.session?.active_task || "新会话";

  return (
    <box flexDirection="row" flexShrink={0} minWidth={0} paddingTop={1}>
      <text fg={theme.text} wrapMode="none" truncate>
        <span style={{ fg: theme.text, attributes: textAttributes.muted }}>{">_ "}</span>
        <b>cb-agent</b>
        <Show when={mode() !== "narrow"}>
          <span style={{ fg: theme.text }}>{`  ${title()}`}</span>
        </Show>
        <Show when={mode() === "wide" && state.session?.session_id}>
          <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
            {`  ${shortSessionId(state.session!.session_id)}`}
          </span>
        </Show>
      </text>
    </box>
  );
}
