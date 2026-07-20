/**
 * ReasoningBlock：可折叠的思考块。
 *
 * 数据层（session.tsx）已把 reasoning_delta 刷成**不可变 chunk**（多条 role=thought）。
 * MessageList/ThoughtGroup 会把连续 chunk 合并后传入 `items`。
 *
 * 渲染策略（对照用户截图：高度很大但只见首行/中段空白）：
 * - 禁止对单个 text 节点反复 `text +=`（那是高度/绘制不同步的根因）
 * - 每个 chunk 用独立 <text>，chunk 内容写入后不再变 → OpenTUI 布局稳定
 * - 不用 Show 卸载正文；折叠时 visible=false 保留节点，避免展开 remount 测量错乱
 * - 布局对齐 AssistantMessage：paddingLeft + bullet 绝对定位
 * - For 以 item.id 为 key（通过 map 到 {id,text}），追加 chunk 只挂新节点
 */

import { createMemo, createSignal, For, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import type { ChatItem } from "../types.js";
import { textAttributes } from "../theme.js";

function summaryFromText(text: string): string {
  const firstLine = text.trim().split("\n")[0] ?? "";
  return firstLine.length > 60 ? firstLine.slice(0, 60) + "…" : firstLine;
}

/** 展示清理：统一换行，压缩 3+ 连续空行。 */
function normalizeChunk(text: string): string {
  if (!text) return "";
  return text
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/\n{3,}/g, "\n\n");
}

export function ReasoningBlock(props: {
  /** 连续 thought item；单条兼容旧路径 */
  items: ChatItem[];
}) {
  const theme = useTheme();
  const [expanded, setExpanded] = createSignal(false);

  const chunks = createMemo(() =>
    props.items
      .map((it) => ({
        id: it.id,
        text: normalizeChunk(it.text ?? ""),
      }))
      .filter((c) => c.text.length > 0),
  );

  const fullText = createMemo(() => chunks().map((c) => c.text).join(""));
  const summary = createMemo(() => summaryFromText(fullText()));
  const charCount = createMemo(() => fullText().length);

  return (
    <box
      position="relative"
      flexDirection="column"
      marginTop={1}
      paddingLeft={2}
      minWidth={0}
      flexShrink={0}
    >
      <box position="absolute" left={0} top={0} width={2}>
        <text fg={theme.text} attributes={textAttributes.muted}>• </text>
      </box>

      <box onMouseUp={() => setExpanded((v) => !v)} flexShrink={0}>
        <text fg={theme.text} attributes={textAttributes.mutedItalic} wrapMode="none">
          <span style={{ fg: theme.agent }}>{expanded() ? "⌄ Thought" : "› Thought"}</span>
          <Show when={!expanded()}>
            <span style={{ fg: theme.text, attributes: textAttributes.mutedItalic }}>
              {`  ${summary()}`}
              {charCount() > 0 ? `  (${charCount()} chars)` : ""}
            </span>
          </Show>
        </text>
      </box>

      {/*
        不用 <Show> 卸载：折叠时 visible=false，展开时再显示。
        每个 chunk 独立 text，写入后不可变 → 不会触发「增长后高度与绘制不同步」。
      */}
      <box
        flexDirection="column"
        minWidth={0}
        flexShrink={0}
        visible={expanded()}
      >
        <For each={chunks()}>
          {(chunk) => (
            <text
              fg={theme.text}
              attributes={textAttributes.muted}
              flexShrink={0}
            >
              {chunk.text}
            </text>
          )}
        </For>
      </box>
    </box>
  );
}
