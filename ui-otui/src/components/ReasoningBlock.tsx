/**
 * ReasoningBlock：可折叠的思考块（M7）。
 *
 * 折叠态（默认）：单行 "▸ Thought 摘要…"，省空间。
 * 展开态：完整思考文本。点击标题切换。
 *
 * 对应 opencode 的 ReasoningPart。cb-agent 的 reasoning_delta 累积进 role="thought" item。
 */

import { createSignal, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import type { ChatItem } from "../types.js";

function summary(text: string): string {
  const firstLine = text.trim().split("\n")[0] ?? "";
  return firstLine.length > 60 ? firstLine.slice(0, 60) + "…" : firstLine;
}

export function ReasoningBlock(props: { item: ChatItem }) {
  const theme = useTheme();
  const [expanded, setExpanded] = createSignal(false);

  return (
    <box flexDirection="column" marginTop={1} paddingLeft={1}>
      <box onMouseUp={() => setExpanded((v) => !v)}>
        <text fg={theme.textMuted}>
          {expanded() ? "▾ " : "▸ "}
          <span style={{ fg: theme.agent }}>Thought</span>
          <Show when={!expanded()}>
            <span style={{ fg: theme.textMuted }}>{`  ${summary(props.item.text)}`}</span>
          </Show>
        </text>
      </box>
      <Show when={expanded()}>
        <box paddingLeft={2}>
          <text fg={theme.textMuted}>{props.item.text}</text>
        </box>
      </Show>
    </box>
  );
}
