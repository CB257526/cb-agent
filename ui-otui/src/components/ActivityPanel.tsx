/**
 * ActivityPanel：后端 stderr 实时面板（M7），Ctrl-O 或 /log 切换显示。
 *
 * 显示 store.stderrLines 的尾部若干行（reducer 已限制 ring buffer 上限 200 行）。
 * 完整日志在 .cbagent/logs/system/gateway-<ts>.log。
 */

import { For, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import { textAttributes } from "../theme.js";

const VISIBLE_LINES = 12;

export function ActivityPanel(props: { logFile: string }) {
  const theme = useTheme();
  const { state } = useSession();
  const lines = () => state.stderrLines.slice(-VISIBLE_LINES);

  return (
    <Show when={state.showActivity}>
      <box
        flexDirection="column"
        flexShrink={0}
        border={["top"]}
        borderColor={theme.border}
        paddingLeft={1}
        paddingRight={1}
      >
        <text fg={theme.text} attributes={textAttributes.muted} wrapMode="none" truncate>
          后端日志（Ctrl-O 关闭） · {props.logFile}
        </text>
        <For each={lines()}>
          {(line) => <text fg={theme.text} attributes={textAttributes.muted}>{line}</text>}
        </For>
      </box>
    </Show>
  );
}
