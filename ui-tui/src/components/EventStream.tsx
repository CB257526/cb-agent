import React from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { ToolBlock } from "./ToolBlock.js";
import { Pane } from "./Pane.js";
import { theme } from "../theme.js";

/** 主对话流：把 ChatItem 列表按角色渲染。 */
export function EventStream({ items }: { items: ChatItem[] }) {
  return (
    <Box flexDirection="column">
      {items.map((it) => (
        <Box key={it.id} marginBottom={1}>
          {renderItem(it)}
        </Box>
      ))}
    </Box>
  );
}

function renderItem(item: ChatItem): React.ReactElement {
  if (item.role === "user") {
    // 用 Pane 给 user 消息加一条蓝色顶 Divider
    return (
      <Pane color={theme.accent}>
        <Box>
          <Text color={theme.accent} bold>you  </Text>
          <Text>{item.text}</Text>
        </Box>
      </Pane>
    );
  }
  if (item.role === "assistant") {
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <Box>
          <Text color={theme.claude} bold>claude  </Text>
        </Box>
        <Box>
          <Text>{item.text}</Text>
        </Box>
      </Box>
    );
  }
  if (item.role === "tool") {
    return <ToolBlock item={item} />;
  }
  return <Text dimColor>{item.text}</Text>;
}
