/**
 * MessageList：把 store.items 渲染进 OpenTUI 的 <scrollbox>。
 *
 * 关键修 bug 点就在这个 scrollbox：
 *   - stickyScroll + stickyStart="bottom"：新内容自动贴底，但用户手动上滑后能停留，
 *     不会被流式更新拽回底部，更不会跳顶（这正是旧 Ink 实现的致命缺陷）。
 *   - scrollbox 渲染到独立屏幕缓冲区，与终端 scrollback 解耦。
 *
 * 各 role 分派到对应组件渲染。assistant 需要知道自己是否是最后一条（流式期间用纯文本、
 * done 后用 markdown），所以 ItemRenderer 透传 isLast。
 */

import { createMemo, ErrorBoundary, For, Switch, Match } from "solid-js";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import { appendOtuiDiagnostic } from "../diagnostics.js";
import { ToolBlock } from "./ToolBlock.js";
import { ReasoningBlock } from "./ReasoningBlock.js";
import { TodoPanel } from "./TodoPanel.js";
import { QuestionPanel } from "./QuestionPanel.js";
import { AssistantMessage } from "./AssistantMessage.js";
import { SubagentPanel } from "./SubagentPanel.js";
import type { ChatItem } from "../types.js";
import { createMarkdownSyntaxStyle, textAttributes } from "../theme.js";

const planSyntaxStyle = createMarkdownSyntaxStyle();

function PlanPanel(props: { item: ChatItem }) {
  const theme = useTheme();
  const status = () => props.item.planStatus ?? "idle";
  const color = () =>
    status() === "approved" ? theme.success :
    status() === "rejected" ? theme.error :
    status() === "pending" ? theme.warning :
    theme.info;

  return (
    <box
      position="relative"
      flexDirection="column"
      paddingLeft={2}
      minWidth={0}
      marginTop={1}
    >
      {/* 与助手消息相同，Plan 的前缀不参与 Markdown 宽度计算。 */}
      <box position="absolute" left={0} top={0} width={2}>
        <text fg={color()}>• </text>
      </box>
      <text fg={color()}>
        <b>Plan</b>
        <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
          {props.item.planRevision ? `  rev ${props.item.planRevision}` : ""}
          {`  ${status()}`}
        </span>
      </text>
      <markdown
        content={props.item.text}
        syntaxStyle={planSyntaxStyle}
        fg={theme.markdownText}
        bg={theme.background}
      />
      {status() === "pending" ? (
        <text fg={theme.text} attributes={textAttributes.muted}>
          /plan approve  或  /plan reject &lt;feedback&gt;
        </text>
      ) : null}
    </box>
  );
}

function ItemRenderer(props: { item: ChatItem; isLast: boolean }) {
  const theme = useTheme();
  const item = () => props.item;

  return (
    <Switch>
      <Match when={item().role === "user"}>
        <box flexDirection="row" marginTop={1}>
          <box width={2} flexShrink={0}>
            <text fg={theme.accent} attributes={textAttributes.selected}>› </text>
          </box>
          <text fg={theme.text}>{item().text}</text>
        </box>
      </Match>

      <Match when={item().role === "assistant"}>
        <AssistantMessage item={item()} isLast={props.isLast} />
      </Match>

      <Match when={item().role === "thought"}>
        <ReasoningBlock item={item()} />
      </Match>

      <Match when={item().role === "tool"}>
        <ToolBlock item={item()} />
      </Match>

      <Match when={item().role === "subagent"}>
        <SubagentPanel item={item()} />
      </Match>

      <Match when={item().role === "todo"}>
        <TodoPanel item={item()} />
      </Match>

      <Match when={item().role === "plan"}>
        <PlanPanel item={item()} />
      </Match>

      <Match when={item().role === "ask_question"}>
        <QuestionPanel item={item()} />
      </Match>

      <Match when={item().role === "system"}>
        <box flexDirection="row" marginTop={1}>
          <box width={2} flexShrink={0}>
            <text fg={theme.text} attributes={textAttributes.muted}>• </text>
          </box>
          <text fg={theme.text} attributes={textAttributes.muted}>{item().text}</text>
        </box>
      </Match>
    </Switch>
  );
}

function ItemError(props: { error: unknown }) {
  const theme = useTheme();
  const message = props.error instanceof Error ? props.error.message : String(props.error);
  appendOtuiDiagnostic("message item render failed", props.error);
  return (
    <box paddingLeft={1} marginTop={1}>
      <text fg={theme.error}>UI 渲染该消息失败：{message}</text>
    </box>
  );
}

export function MessageList() {
  const theme = useTheme();
  const { state } = useSession();
  const visibleItems = createMemo(() =>
    state.items.filter(
      (item) =>
        item.role !== "ask_question"
        || item.answered
        || item.questionId !== state.activeQuestionId,
    ),
  );

  return (
    <scrollbox
      stickyScroll={true}
      stickyStart="bottom"
      flexGrow={1}
      minHeight={0}
      verticalScrollbarOptions={{
        visible: true,
        trackOptions: {
          backgroundColor: theme.background,
          foregroundColor: theme.border,
        },
      }}
    >
      <For each={visibleItems()}>
        {(item, index) => (
          <ErrorBoundary fallback={(error) => <ItemError error={error} />}>
            <ItemRenderer item={item} isLast={index() === visibleItems().length - 1} />
          </ErrorBoundary>
        )}
      </For>
    </scrollbox>
  );
}
