/**
 * MessageList：把 store.items 渲染进 OpenTUI 的 <scrollbox>。
 *
 * 关键修 bug 点就在这个 scrollbox：
 *   - stickyScroll + stickyStart="bottom"：新内容自动贴底，但用户手动上滑后能停留，
 *     不会被流式更新拽回底部，更不会跳顶（这正是旧 Ink 实现的致命缺陷）。
 *   - scrollbox 渲染到独立屏幕缓冲区，与终端 scrollback 解耦。
 *
 * Thought：session 把 reasoning 刷成不可变 chunk（多条 role=thought）。
 * For 仍按 store item 稳定 identity 迭代；连续 thought 只在首条渲染
 * ThoughtGroup，组内用 createMemo 响应式收集后续 chunk，避免：
 *   - N 个「› Thought」标题
 *   - Solid For 不重跑已有项导致 props.items 过期
 *
 * Ctrl-L 清屏：不删 items，只在末尾插入 clear_viewport 占位（高度≈可视区），再强制
 * 滚到底。主界面看起来被清空，但上滑仍能看到之前的对话。
 */

import { createMemo, ErrorBoundary, For, Match, onCleanup, onMount, Show, Switch } from "solid-js";
import type { ScrollBoxRenderable } from "@opentui/core";
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

/**
 * 连续 thought 的视觉入口。
 * 挂在组内第一条 thought 上；从 store 响应式收集直到非 thought。
 * 后续 chunk 的 ItemRenderer 返回 null。
 */
function ThoughtGroup(props: { headId: string }) {
  const { state } = useSession();

  const group = createMemo(() => {
    const items = state.items;
    const start = items.findIndex((it) => it.id === props.headId);
    if (start < 0) return [] as ChatItem[];
    // 若前一条也是 thought，说明自己不是 head（理论上不该挂载）
    if (start > 0 && items[start - 1]?.role === "thought") return [] as ChatItem[];
    const out: ChatItem[] = [];
    for (let i = start; i < items.length; i++) {
      if (items[i].role !== "thought") break;
      out.push(items[i]);
    }
    return out;
  });

  return (
    <Show when={group().length > 0}>
      <ReasoningBlock items={group()} />
    </Show>
  );
}

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

function ItemRenderer(props: {
  item: ChatItem;
  isLast: boolean;
  index: number;
  items: ChatItem[];
}) {
  const theme = useTheme();
  const item = () => props.item;

  const isThoughtHead = createMemo(() => {
    if (item().role !== "thought") return false;
    const prev = props.index > 0 ? props.items[props.index - 1] : null;
    return !prev || prev.role !== "thought";
  });

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

      <Match when={item().role === "thought" && isThoughtHead()}>
        <ThoughtGroup headId={item().id} />
      </Match>

      {/* 连续 thought 的非首条：占位 null，内容由 head 的 ThoughtGroup 展示 */}
      <Match when={item().role === "thought"}>
        <box height={0} flexShrink={0} />
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

      <Match when={item().role === "clear_viewport"}>
        {/* 纯占位：把历史内容顶出可视区。高度由 clearHeight 指定，至少 1 行。 */}
        <box height={Math.max(1, item().clearHeight ?? 1)} flexShrink={0} />
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
  const { state, registerMessageListScroller } = useSession();
  let scrollRef: ScrollBoxRenderable | undefined;

  const visibleItems = createMemo(() =>
    state.items.filter(
      (item) =>
        item.role !== "ask_question"
        || item.answered
        || item.questionId !== state.activeQuestionId,
    ),
  );

  const scrollToBottom = () => {
    const box = scrollRef;
    if (!box) return;
    // sticky 在用户手动上滑后可能断开；Ctrl-L 需要强制贴底。
    // scrollHeight 是内容总高，设 scrollTop 到内容底部即可把最新占位顶入视口。
    try {
      box.scrollTop = Math.max(0, box.scrollHeight);
    } catch {
      // 布局尚未就绪时忽略；clearViewport 会再尝试一次。
    }
  };

  const getViewportHeight = () => {
    const h = scrollRef?.height;
    return typeof h === "number" && Number.isFinite(h) && h > 0 ? Math.floor(h) : 0;
  };

  onMount(() => {
    registerMessageListScroller({ scrollToBottom, getViewportHeight });
  });
  onCleanup(() => {
    registerMessageListScroller(null);
  });

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
      ref={(r: ScrollBoxRenderable) => {
        scrollRef = r;
      }}
    >
      <For each={visibleItems()}>
        {(item, index) => (
          <ErrorBoundary fallback={(error) => <ItemError error={error} />}>
            <ItemRenderer
              item={item}
              index={index()}
              items={visibleItems()}
              isLast={index() === visibleItems().length - 1}
            />
          </ErrorBoundary>
        )}
      </For>
    </scrollbox>
  );
}
