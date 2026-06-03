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

  // diff 数据提取与可视化块构建
  const diffData = useMemo(() => extractDiff(result), [result]);
  const hasDiff = diffData !== null;
  const diffBlocks = useMemo(
    () => (diffData ? buildDiffBlocks(diffData.text).slice(0, DIFF_MAX_BLOCKS) : []),
    [diffData],
  );

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
        {hasDiff && (
          <Box flexDirection="row" marginTop={1}>
            <Box width={5}><Text dimColor>DIFF</Text></Box>
            <Box flexDirection="column" flexGrow={1}>
              {diffBlocks.map((block, bi) => (
                <Box key={bi} flexDirection="column">
                  {block.kind === "context" && (
                    block.lines.map((line, li) => (
                      <Text key={li} dimColor>{line}</Text>
                    ))
                  )}
                  {block.kind === "removal" && (
                    block.lines.map((line, li) => (
                      <Text key={li} color={theme.error} backgroundColor="#3d1f28">{line}</Text>
                    ))
                  )}
                  {block.kind === "addition" && (
                    block.lines.map((line, li) => (
                      <Text key={li} color={theme.success} backgroundColor="#1a3a2a">{line}</Text>
                    ))
                  )}
                  {block.kind === "modify" && (
                    block.pairs.map((pair, pi) => (
                      <Box key={pi} flexDirection="column">
                        {/* 删除行（红底） */}
                        {pair.old && (
                          <Text backgroundColor="#3d1f28">
                            <Text color={theme.error}>- </Text>
                            {pair.wordDiff ? (
                              <>
                                <Text dimColor>{pair.wordDiff.oldPrefix}</Text>
                                <Text color={theme.error} bold backgroundColor="#5a2030">{pair.wordDiff.oldChanged}</Text>
                                <Text dimColor>{pair.wordDiff.oldSuffix}</Text>
                              </>
                            ) : (
                              <Text color={theme.error}>{pair.old.slice(1)}</Text>
                            )}
                          </Text>
                        )}
                        {/* 新增行（绿底） */}
                        {pair.new && (
                          <Text backgroundColor="#1a3a2a">
                            <Text color={theme.success}>+ </Text>
                            {pair.wordDiff ? (
                              <>
                                <Text dimColor>{pair.wordDiff.newPrefix}</Text>
                                <Text color={theme.success} bold backgroundColor="#1d4a30">{pair.wordDiff.newChanged}</Text>
                                <Text dimColor>{pair.wordDiff.newSuffix}</Text>
                              </>
                            ) : (
                              <Text color={theme.success}>{pair.new.slice(1)}</Text>
                            )}
                          </Text>
                        )}
                      </Box>
                    ))
                  )}
                </Box>
              ))}
              {diffData?.truncated && (
                <Text dimColor>
                  ... [diff 已截断，显示 {diffData.linesShown}/{diffData.linesTotal} 行]
                </Text>
              )}
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
    if (obj && typeof obj === "object") {
      // BashTool 约定：显式 __display__ 字段
      if (typeof obj.__display__ === "string") return obj.__display__;
      // 通用回退：任何工具包含 message 字段时，用作文本预览
      if (typeof obj.message === "string") return obj.message;
    }
  } catch {
    return null;
  }
  return null;
}

// ── Diff 解析与可视化渲染 ──

interface DiffData {
  text: string;
  truncated: boolean;
  linesTotal: number;
  linesShown: number;
}

/** 从 toolResult JSON 中提取 diff 数据。不是 JSON 或没有 diff 字段时返回 null。 */
function extractDiff(result: string): DiffData | null {
  const t = result.trimStart();
  if (!t.startsWith("{")) return null;
  try {
    const obj = JSON.parse(result);
    if (obj && typeof obj === "object" && typeof obj.diff === "string" && obj.diff.length > 0) {
      return {
        text: obj.diff,
        truncated: !!obj.diff_truncated,
        linesTotal: typeof obj.diff_lines_total === "number" ? obj.diff_lines_total : 0,
        linesShown: typeof obj.diff_lines_shown === "number" ? obj.diff_lines_shown : 0,
      };
    }
  } catch {
    return null;
  }
  return null;
}

// ── 行级标记 ──

type RawLineType = "header" | "hunk" | "add" | "remove" | "context";

function classifyLine(line: string): RawLineType {
  if (line.startsWith("--- ") || line.startsWith("+++ ")) return "header";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}

// ── 词级 diff ──

interface LineDiff {
  oldPrefix: string;
  oldChanged: string;
  oldSuffix: string;
  newPrefix: string;
  newChanged: string;
  newSuffix: string;
}

/** 对一对 "删除/新增" 行做简单的词级差异定位。
 *
 *  算法：找最长公共前缀 + 最长公共后缀，中间部分即为变更区域。
 *  适合大多数单行修改场景（重命名变量、改字符串等）。
 *  当变更比例超过 60% 时返回 null，由上层按整行替换渲染。 */
function computeLineDiff(oldLine: string, newLine: string): LineDiff | null {
  const minLen = Math.min(oldLine.length, newLine.length);

  // 公共前缀
  let prefixLen = 0;
  while (prefixLen < minLen && oldLine[prefixLen] === newLine[prefixLen]) {
    prefixLen++;
  }

  // 公共后缀（不越入前缀区域）
  let suffixLen = 0;
  while (
    suffixLen < minLen - prefixLen &&
    oldLine[oldLine.length - 1 - suffixLen] === newLine[newLine.length - 1 - suffixLen]
  ) {
    suffixLen++;
  }

  const oldChanged = oldLine.slice(prefixLen, oldLine.length - suffixLen);
  const newChanged = newLine.slice(prefixLen, newLine.length - suffixLen);

  // 变更比例 > 60% → 放弃词级高亮，按整行替换渲染
  const changeRatio = Math.max(oldChanged.length / Math.max(oldLine.length, 1),
                               newChanged.length / Math.max(newLine.length, 1));
  if (changeRatio > 0.6) return null;

  return {
    oldPrefix: oldLine.slice(0, prefixLen),
    oldChanged,
    oldSuffix: oldLine.slice(oldLine.length - suffixLen),
    newPrefix: newLine.slice(0, prefixLen),
    newChanged,
    newSuffix: newLine.slice(newLine.length - suffixLen),
  };
}

// ── 可视化块（一个块 = 一段上下文 / 一组增删改） ──

type DiffBlock =
  | { kind: "context"; lines: string[] }
  | { kind: "removal"; lines: string[] }
  | { kind: "addition"; lines: string[] }
  | { kind: "modify"; pairs: { old: string; new: string; wordDiff: LineDiff | null }[] };

/** 把原始 diff 文本转为可视化块列表。
 *
 *  处理步骤：
 *  1. 按行解析 → 去掉 header / hunk 行
 *  2. 将相邻的 -/+ 行配对为 modify 块
 *  3. 剩余的 - 行 → removal 块，+ 行 → addition 块
 *  4. 上下文行合并为一个 context 块
 */
function buildDiffBlocks(rawDiff: string): DiffBlock[] {
  const rawLines = rawDiff.split("\n");
  const blocks: DiffBlock[] = [];

  // 阶段 1：收集有效行（丢掉 header/hunk），标记类型
  interface TaggedLine { type: RawLineType; text: string; content: string }
  const tagged: TaggedLine[] = [];
  for (const line of rawLines) {
    const type = classifyLine(line);
    if (type === "header" || type === "hunk") continue;
    // 去掉 +/- 前缀，保留原文字用于词级 diff
    const content = (type === "add" || type === "remove") ? line.slice(1) : line;
    tagged.push({ type, text: line, content });
  }

  // 阶段 2：配对 -/+ 行，生成 modify / removal / addition / context 块
  let i = 0;
  let ctxBuf: string[] = [];

  function flushCtx() {
    if (ctxBuf.length > 0) {
      blocks.push({ kind: "context", lines: ctxBuf.slice() });
      ctxBuf = [];
    }
  }

  while (i < tagged.length) {
    const cur = tagged[i];

    if (cur.type === "context") {
      ctxBuf.push(cur.text);
      i++;
      continue;
    }

    // 尝试配对连续的 - 行和 + 行 → modify 块
    if (cur.type === "remove") {
      const removals: TaggedLine[] = [];
      while (i < tagged.length && tagged[i].type === "remove") {
        removals.push(tagged[i]);
        i++;
      }
      const additions: TaggedLine[] = [];
      while (i < tagged.length && tagged[i].type === "add") {
        additions.push(tagged[i]);
        i++;
      }

      flushCtx();

      const maxPairs = Math.max(removals.length, additions.length);
      const pairs: { old: string; new: string; wordDiff: LineDiff | null }[] = [];
      for (let j = 0; j < maxPairs; j++) {
        const oldContent = j < removals.length ? removals[j].content : "";
        const newContent = j < additions.length ? additions[j].content : "";
        pairs.push({
          old: j < removals.length ? removals[j].text : "",
          new: j < additions.length ? additions[j].text : "",
          wordDiff: (oldContent && newContent) ? computeLineDiff(oldContent, newContent) : null,
        });
      }
      blocks.push({ kind: "modify", pairs });
      continue;
    }

    // 孤立的 + 行 → addition 块
    if (cur.type === "add") {
      flushCtx();
      const lines: string[] = [];
      while (i < tagged.length && tagged[i].type === "add") {
        lines.push(tagged[i].text);
        i++;
      }
      blocks.push({ kind: "addition", lines });
      continue;
    }

    i++;
  }

  flushCtx();
  return blocks;
}

/** 前端 diff 渲染安全截断行数（后端已做截断，此处为兜底）。 */
const DIFF_MAX_BLOCKS = 40;
