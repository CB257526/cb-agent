import { Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import type { ChatItem } from "../types.js";


function compactArgs(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return "";
  try {
    const text = JSON.stringify(args);
    return text.length > 180 ? `${text.slice(0, 177)}...` : text;
  } catch {
    return "";
  }
}


/** 聚合同一个 task_id 的启动、工具进度和完成状态。 */
export function SubagentPanel(props: { item: ChatItem }) {
  const theme = useTheme();
  const color = () => {
    if (props.item.subagentStatus === "completed") return theme.success;
    if (props.item.subagentStatus === "cancelled" || props.item.subagentStatus === "orphaned") return theme.warning;
    if (props.item.subagentError || props.item.subagentStatus === "failed" || props.item.subagentStatus === "error") return theme.error;
    return theme.info;
  };
  const args = () => compactArgs(props.item.subagentToolArgs);

  return (
    <box
      flexDirection="column"
      border={["left"]}
      borderColor={color()}
      paddingLeft={1}
      marginTop={1}
    >
      <text fg={color()}>
        <b>subagent {props.item.subagentType ?? "unknown"}</b>
        <span style={{ fg: theme.textMuted }}>
          {`  ${props.item.subagentStatus ?? "running"}`}
          {props.item.subagentPhase ? ` / ${props.item.subagentPhase}` : ""}
        </span>
      </text>
      <Show when={props.item.subagentDescription}>
        <text fg={theme.text}>{props.item.subagentDescription}</text>
      </Show>
      <Show when={props.item.subagentToolName}>
        <text fg={theme.suggestion}>tool  {props.item.subagentToolName}</text>
      </Show>
      <Show when={args()}>
        <text fg={theme.textMuted}>{args()}</text>
      </Show>
      <Show when={props.item.subagentMessage}>
        <text fg={theme.textMuted}>{props.item.subagentMessage}</text>
      </Show>
      <text fg={theme.textMuted}>
        {`tools ${props.item.subagentToolUses ?? 0}  tokens ${props.item.subagentTokens ?? 0}`}
        {(props.item.subagentActiveTools ?? 0) > 0 ? `  active ${props.item.subagentActiveTools}` : ""}
        {props.item.subagentRounds !== undefined ? `  rounds ${props.item.subagentRounds}` : ""}
        {props.item.subagentDuration !== undefined ? `  ${props.item.subagentDuration.toFixed(2)}s` : ""}
      </text>
      <Show when={props.item.text}>
        <text fg={theme.text}>{props.item.text}</text>
      </Show>
      <Show when={props.item.subagentOutputPath}>
        <text fg={theme.textMuted}>{props.item.subagentOutputPath}</text>
      </Show>
    </box>
  );
}
