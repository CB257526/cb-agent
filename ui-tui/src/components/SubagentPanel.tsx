import React from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { Pane } from "./Pane.js";
import { theme } from "../theme.js";


function statusColor(item: ChatItem): string {
  if (item.subagentStatus === "completed") return theme.success;
  if (item.subagentStatus === "cancelled" || item.subagentStatus === "orphaned") return theme.warning;
  if (item.subagentError || item.subagentStatus === "failed" || item.subagentStatus === "error") return theme.error;
  return theme.info;
}


function compactArgs(args?: Record<string, unknown>): string {
  if (!args || Object.keys(args).length === 0) return "";
  try {
    const text = JSON.stringify(args);
    return text.length > 180 ? `${text.slice(0, 177)}...` : text;
  } catch {
    return "";
  }
}


/** 同一个 task_id 的子代理生命周期会聚合到这一块，不为每次工具进度新增卡片。 */
export function SubagentPanel({ item }: { item: ChatItem }) {
  const color = statusColor(item);
  const args = compactArgs(item.subagentToolArgs);
  return (
    <Pane color={color}>
      <Box flexDirection="column">
        <Box>
          <Text color={color} bold>subagent {item.subagentType ?? "unknown"}</Text>
          <Text dimColor>  {item.subagentStatus ?? "running"}</Text>
          {item.subagentPhase ? <Text dimColor> / {item.subagentPhase}</Text> : null}
        </Box>
        {item.subagentDescription ? <Text>{item.subagentDescription}</Text> : null}
        {item.subagentToolName ? (
          <Box flexDirection="column" marginTop={1}>
            <Text color={theme.suggestion}>tool  {item.subagentToolName}</Text>
            {args ? <Text dimColor>{args}</Text> : null}
          </Box>
        ) : null}
        {item.subagentMessage ? <Text dimColor>{item.subagentMessage}</Text> : null}
        <Text dimColor>
          tools {item.subagentToolUses ?? 0}  tokens {item.subagentTokens ?? 0}
          {(item.subagentActiveTools ?? 0) > 0 ? `  active ${item.subagentActiveTools}` : ""}
          {item.subagentRounds !== undefined ? `  rounds ${item.subagentRounds}` : ""}
          {item.subagentDuration !== undefined ? `  ${item.subagentDuration.toFixed(2)}s` : ""}
        </Text>
        {item.text ? <Text>{item.text}</Text> : null}
        {item.subagentOutputPath ? <Text dimColor>{item.subagentOutputPath}</Text> : null}
      </Box>
    </Pane>
  );
}
