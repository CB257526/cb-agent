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

import { For, Switch, Match } from "solid-js";
import { SyntaxStyle } from "@opentui/core";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import { ToolBlock } from "./ToolBlock.js";
import { ReasoningBlock } from "./ReasoningBlock.js";
import { TodoPanel } from "./TodoPanel.js";
import { QuestionPanel } from "./QuestionPanel.js";
import { AssistantMessage } from "./AssistantMessage.js";
import type { ChatItem } from "../types.js";

const planSyntaxStyle = SyntaxStyle.create();

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
      flexDirection="column"
      border={["left"]}
      borderColor={color()}
      paddingLeft={1}
      marginTop={1}
    >
      <text fg={color()}>
        <b>plan</b>
        <span style={{ fg: theme.textMuted }}>
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
        <text fg={theme.textMuted}>/plan approve  or  /plan reject &lt;feedback&gt;</text>
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
        <box border={["left"]} borderColor={theme.accent} paddingLeft={1} marginTop={1}>
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
        <box paddingLeft={1} marginTop={1}>
          <text fg={theme.textMuted}>{item().text}</text>
        </box>
      </Match>
    </Switch>
  );
}

export function MessageList() {
  const theme = useTheme();
  const { state } = useSession();

  return (
    <scrollbox
      stickyScroll={true}
      stickyStart="bottom"
      flexGrow={1}
      verticalScrollbarOptions={{
        visible: true,
        trackOptions: {
          backgroundColor: theme.backgroundElement,
          foregroundColor: theme.border,
        },
      }}
    >
      <box height={1} />
      <For each={state.items}>
        {(item, index) => (
          <ItemRenderer item={item} isLast={index() === state.items.length - 1} />
        )}
      </For>
    </scrollbox>
  );
}
