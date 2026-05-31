import React, { useMemo } from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { StatusIcon } from "./StatusIcon.js";
import { theme } from "../theme.js";

/**
 * 工具调用块。
 *
 * 折叠态：⏵ name(args 摘要)  ✓ 0.02s
 * 展开态（默认）：上方一行标题 + 一个外框，框内分两段——
 *   IN  : 完整 args（JSON 漂亮打印 / bash 命令直接显示）
 *   OUT : 工具结果，截前 800 字
 *
 * cb-agent 工具结果是 JSON 字符串或 shell stdout/stderr，长度可能从 50 字到几千字。
 * 这里截 800 字给一个"看得见但不刷屏"的预览，完整内容用户可以从 stderr 日志或
 * result.* 文件查。
 */

const RESULT_MAX = 800;
const ARGS_MAX = 400;

export const ToolBlock = React.memo(function ToolBlock({ item }: { item: ChatItem }) {
  const argsBrief = summarizeArgs(item.toolArgs);
  const status = item.toolDone
    ? item.toolError
      ? <Text color={theme.error}><StatusIcon status="error" withSpace />{item.toolDuration?.toFixed(2)}s</Text>
      : <Text color={theme.success}><StatusIcon status="success" withSpace />{item.toolDuration?.toFixed(2)}s</Text>
    : <Text color={theme.warning}><StatusIcon status="loading" withSpace />running</Text>;

  // 折叠态：保持原来的紧凑单行
  if (item.collapsed) {
    return (
      <Box flexDirection="column" marginY={0}>
        <Box>
          <Text color={theme.suggestion}>⏵ </Text>
          <Text bold>{item.toolName}</Text>
          <Text dimColor>({argsBrief})  </Text>
          {status}
        </Box>
      </Box>
    );
  }

  // 展开态：标题 + IN/OUT 框
  const argsFull = useMemo(() => formatArgsFull(item.toolArgs), [item.toolArgs]);
  const result = item.toolResult ?? "";
  const renderResult = useMemo(() => extractDisplay(result) ?? result, [result]);
  const argsLines = useMemo(
    () => (argsFull ? truncate(argsFull, ARGS_MAX).split("\n") : []),
    [argsFull],
  );
  const resultLines = useMemo(
    () => (renderResult ? truncate(renderResult, RESULT_MAX).split("\n") : []),
    [renderResult],
  );
  const hasResult = renderResult.length > 0;

  return (
    <Box flexDirection="column" marginY={0}>
      <Box>
        <Text color={theme.suggestion}>● </Text>
        <Text bold color={theme.suggestion}>{item.toolName}</Text>
        <Text dimColor>  ({argsBrief})  </Text>
        {status}
      </Box>
      <Box marginLeft={2} flexDirection="column" borderStyle="single" borderColor={theme.bashBorder} paddingX={1}>
        {argsFull && (
          <Box flexDirection="row">
            <Box width={5}><Text dimColor>IN</Text></Box>
            <Box flexDirection="column" flexGrow={1}>
              {argsLines.map((line, i) => (
                <Text key={i}>{line}</Text>
              ))}
            </Box>
          </Box>
        )}
        {hasResult && (
          <Box flexDirection="row" marginTop={argsFull ? 1 : 0}>
            <Box width={5}><Text dimColor>OUT</Text></Box>
            <Box flexDirection="column" flexGrow={1}>
              {resultLines.map((line, i) => (
                <Text key={i} color={item.toolError ? theme.error : undefined}>{line}</Text>
              ))}
            </Box>
          </Box>
        )}
      </Box>
    </Box>
  );
});

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

/**
 * 完整参数渲染：
 * - 单字段且为字符串（最常见的 bash command / 命令行）→ 直出原文
 * - 其他 → JSON.stringify(2) 漂亮打印
 */
function formatArgsFull(args?: Record<string, unknown>): string {
  if (!args) return "";
  const entries = Object.entries(args);
  if (entries.length === 0) return "";
  if (entries.length === 1 && typeof entries[0][1] === "string") {
    return entries[0][1] as string;
  }
  try {
    return JSON.stringify(args, null, 2);
  } catch {
    return String(args);
  }
}

function truncate(s: string, max: number): string {
  if (s.length <= max) return s;
  return s.slice(0, max) + `\n... [+${s.length - max} chars truncated, see ~/.cb-agent/logs]`;
}

/**
 * 工具结果若是 JSON 且包含 __display__ 字段，提取作为 UI 预览。
 * 当前 BashTool 用此约定避免把整段结构化 JSON 直接刷到屏幕上。
 * 不是 JSON 或字段缺失时返回 null，由上层 fallback 到原文渲染。
 */
function extractDisplay(s: string): string | null {
  const t = s.trimStart();
  if (!t.startsWith("{")) return null;
  try {
    const obj = JSON.parse(s);
    if (obj && typeof obj === "object" && typeof obj.__display__ === "string") {
      return obj.__display__;
    }
  } catch {
    return null;
  }
  return null;
}
