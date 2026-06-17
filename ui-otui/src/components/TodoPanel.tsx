/**
 * TodoPanel：渲染 todo_list_updated 的快照卡片（M7）。
 *
 * 每次 todo 写入产生一个 role="todo" item（reducer 已处理），里面带该次写入后的全量列表。
 * 状态用图标区分：completed ✓ / in_progress ● / cancelled ✗ / pending ○。
 */

import { For } from "solid-js";
import { useTheme } from "../context/theme.js";
import type { ChatItem, TodoItem } from "../types.js";

function statusIcon(status: TodoItem["status"]): string {
  switch (status) {
    case "completed":
      return "✓";
    case "in_progress":
      return "●";
    case "cancelled":
      return "✗";
    default:
      return "○";
  }
}

export function TodoPanel(props: { item: ChatItem }) {
  const theme = useTheme();
  const items = () => props.item.todoItems ?? [];

  const iconColor = (status: TodoItem["status"]) =>
    status === "completed"
      ? theme.success
      : status === "in_progress"
        ? theme.warning
        : status === "cancelled"
          ? theme.error
          : theme.textMuted;

  return (
    <box
      flexDirection="column"
      marginTop={1}
      border={["left"]}
      borderColor={theme.permission}
      paddingLeft={1}
    >
      <text fg={theme.textMuted}>Todos</text>
      <For each={items()}>
        {(todo) => (
          <text fg={todo.status === "cancelled" ? theme.textMuted : theme.text}>
            <span style={{ fg: iconColor(todo.status) }}>{statusIcon(todo.status)} </span>
            {todo.content}
          </text>
        )}
      </For>
    </box>
  );
}
