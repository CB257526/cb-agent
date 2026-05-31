import React from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { ToolBlock } from "./ToolBlock.js";

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
    return (
      <Box>
        <Text color="cyan" bold>you  </Text>
        <Text>{item.text}</Text>
      </Box>
    );
  }
  if (item.role === "assistant") {
    return <Text>{item.text}</Text>;
  }
  if (item.role === "tool") {
    return <ToolBlock item={item} />;
  }
  return <Text color="gray">{item.text}</Text>;
}
