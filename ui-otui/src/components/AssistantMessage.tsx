/**
 * AssistantMessage（M8）：助手回答正文。
 *
 * 渲染策略（沿用旧实现，避免半截 markdown 抖动）：
 *   - busy 流式期间：纯文本逐字追加（plain text），开销低、不抖。
 *   - done 之后：用 OpenTUI 内置 <markdown> 解析渲染（标题/代码块/列表/表格）。
 *
 * 用 store.busy 判断当前是否仍在流式输出。<markdown> 需要一个 SyntaxStyle，
 * 用 SyntaxStyle.create() 取默认样式即可（M8 暂不接主题色语法高亮）。
 */

import { createMemo, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import type { ChatItem } from "../types.js";
import { createMarkdownSyntaxStyle, textAttributes } from "../theme.js";

// 默认语法样式，整个进程共用一份
const syntaxStyle = createMarkdownSyntaxStyle();

export function AssistantMessage(props: { item: ChatItem; isLast: boolean }) {
  const theme = useTheme();
  const { state } = useSession();

  // 仍在流式输出最后一条 assistant：用纯文本；否则用 markdown 渲染
  const streaming = createMemo(() => state.busy && props.isLast);

  return (
    <box position="relative" flexDirection="column" marginTop={1} paddingLeft={2} minWidth={0}>
      {/* Markdown 需要直接获得剩余整行宽度，前缀使用绝对定位避免参与横向测量。 */}
      <box position="absolute" left={0} top={0} width={2}>
        <text fg={theme.text} attributes={textAttributes.muted}>• </text>
      </box>
      <Show
        when={!streaming()}
        fallback={<text fg={theme.text}>{props.item.text}</text>}
      >
        <markdown
          content={props.item.text}
          syntaxStyle={syntaxStyle}
          fg={theme.markdownText}
          bg={theme.background}
        />
      </Show>
    </box>
  );
}
