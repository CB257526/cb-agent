/**
 * TodoPanel：todo 列表卡片，对应后端 TodoListUpdated 事件。
 *
 * 视觉风格仿 Claude Code 截图：
 *   ● Update Todos                          ← 标题 + 圆点
 *   ☑ 已完成项                              ← completed，删除线
 *   ⊡ 当前进行项                            ← in_progress，高亮 / *
 *   ☐ 待开始项                              ← pending
 *   ⊠ 已取消项                              ← cancelled，dim
 *
 * 不接收输入，纯展示。每次 todo 写入会生成一张独立卡片（按用户偏好"每次都新增一张"），
 * 旧卡片不会变更。
 */

import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import { TodoItem } from "../types.js";

const MARK = {
  completed: "☑",
  in_progress: "⊡",
  pending: "☐",
  cancelled: "⊠",
} as const;

export function TodoPanel({ items }: { items: TodoItem[] }) {
  return (
    <Box flexDirection="column">
      <Box>
        <Text color={theme.success}>● </Text>
        <Text bold>Update Todos</Text>
      </Box>
      <Box flexDirection="column" paddingLeft={2}>
        {items.length === 0 ? (
          <Text dimColor>(空)</Text>
        ) : (
          items.map((it) => <TodoRow key={it.id} item={it} />)
        )}
      </Box>
    </Box>
  );
}

function TodoRow({ item }: { item: TodoItem }) {
  const mark = MARK[item.status] ?? "·";
  if (item.status === "completed") {
    return (
      <Box>
        <Text color={theme.success}>{mark} </Text>
        <Text dimColor strikethrough>{item.content}</Text>
      </Box>
    );
  }
  if (item.status === "in_progress") {
    return (
      <Box>
        <Text color={theme.warning} bold>{mark} </Text>
        <Text bold>{item.content}</Text>
      </Box>
    );
  }
  if (item.status === "cancelled") {
    return (
      <Box>
        <Text dimColor>{mark} </Text>
        <Text dimColor strikethrough>{item.content}</Text>
      </Box>
    );
  }
  // pending
  return (
    <Box>
      <Text dimColor>{mark} </Text>
      <Text>{item.content}</Text>
    </Box>
  );
}
