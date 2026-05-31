import React from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";

/**
 * 工具调用块。折叠时显示 `⏵ name(args 摘要)  ✓ 0.02s`，展开时多显示完整 args 和 result。
 *
 * cb-agent 工具结果是 JSON 字符串，长度可能从 50 字到几千字不等——这里只截前 600 字，
 * 完整内容用户可以从 stderr 日志或者 result.* 文件查。
 */
export function ToolBlock({ item }: { item: ChatItem }) {
  const argsBrief = summarizeArgs(item.toolArgs);
  const status = item.toolDone
    ? item.toolError
      ? <Text color="red">✗ {item.toolDuration?.toFixed(2)}s</Text>
      : <Text color="green">✓ {item.toolDuration?.toFixed(2)}s</Text>
    : <Text color="yellow">… running</Text>;

  return (
    <Box flexDirection="column" marginY={0}>
      <Box>
        <Text color="cyan">⏵ </Text>
        <Text bold>{item.toolName}</Text>
        <Text color="gray">({argsBrief})  </Text>
        {status}
      </Box>
      {!item.collapsed && item.toolResult && (
        <Box marginLeft={2} flexDirection="column">
          <Text color="gray">{truncate(item.toolResult, 600)}</Text>
        </Box>
      )}
    </Box>
  );
}

function summarizeArgs(args?: Record<string, unknown>): string {
  if (!args) return "";
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  const parts = entries.slice(0, 3).map(([k, v]) => `${k}=${formatValue(v)}`);
  if (entries.length > 3) parts.push("...");
  return parts.join(", ");
}

function formatValue(v: unknown): string {
  if (typeof v === "string") {
    return JSON.stringify(v.length > 40 ? v.slice(0, 40) + "..." : v);
  }
  if (Array.isArray(v)) return `[${v.length}]`;
  if (typeof v === "object" && v !== null) return "{...}";
  return String(v);
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n... [+${s.length - max} chars truncated, see ~/.cb-agent/logs]`;
}
