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
import { textAttributes } from "../theme.js";

function summary(text: string): string {
  const firstLine = text.trim().split("\n")[0] ?? "";
  return firstLine.length > 60 ? firstLine.slice(0, 60) + "…" : firstLine;
}

export function ReasoningBlock(props: { item: ChatItem }) {
  const theme = useTheme();
  const [expanded, setExpanded] = createSignal(false);

  return (
    <box flexDirection="row" marginTop={1}>
      <box width={2} flexShrink={0}>
        <text fg={theme.text} attributes={textAttributes.muted}>• </text>
      </box>
      <box flexDirection="column" flexGrow={1} minWidth={0}>
        <box onMouseUp={() => setExpanded((v) => !v)}>
          <text fg={theme.text} attributes={textAttributes.mutedItalic}>
            <span style={{ fg: theme.agent }}>{expanded() ? "⌄ Thought" : "› Thought"}</span>
            <Show when={!expanded()}>
              <span style={{ fg: theme.text, attributes: textAttributes.mutedItalic }}>
                {`  ${summary(props.item.text)}`}
              </span>
            </Show>
          </text>
        </box>
        <Show when={expanded()}>
          <box flexDirection="row">
            <text fg={theme.text} attributes={textAttributes.muted}>│ </text>
            <text fg={theme.text} attributes={textAttributes.mutedItalic}>{props.item.text}</text>
          </box>
        </Show>
      </box>
    </box>
  );
}
