/**
 * ToolBlock：工具调用块（M4）。
 *
 * 折叠态（默认）：⏵ name(args 摘要)  状态图标 + 耗时
 * 展开态：标题行 + 外框，框内 IN（完整参数）/ OUT（结果，截 800 字）/ DIFF（着色）
 *
 * 点击标题行切换折叠（OpenTUI box 的 onMouseUp）。配对已在 session reducer 里按
 * call_id 完成，这里只负责渲染。
 *
 * 纯函数辅助（summarizeArgs/formatArgsFull/extractDisplay/extractDiff/truncate）
 * 统一处理参数、输出和差异文本。
 */

import { createSignal, For, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import type { ChatItem } from "../types.js";
import { textAttributes } from "../theme.js";

const RESULT_MAX = 800;
const ARGS_MAX = 400;

export function ToolBlock(props: { item: ChatItem }) {
  const theme = useTheme();
  const item = () => props.item;
  // 初始折叠态跟随 reducer 设的 collapsed（默认 true）
  const [collapsed, setCollapsed] = createSignal(props.item.collapsed ?? true);

  const argsBrief = () => summarizeArgs(item().toolArgs);
  const statusColor = () =>
    !item().toolDone ? theme.info : item().toolError ? theme.error : theme.success;
  const statusIcon = () =>
    !item().toolDone ? "○" : item().toolError ? "✗" : "✓";
  const statusText = () =>
    item().toolDone ? `${item().toolDuration?.toFixed(2) ?? "?"}s` : "running";

  const argsFull = () => {
    const f = formatArgsFull(item().toolArgs);
    return f ? truncate(f, ARGS_MAX) : "";
  };
  const result = () => stringifyToolValue(item().toolResult ?? "");
  const renderResult = () => {
    const r = result();
    const display = extractDisplay(r) ?? r;
    return display ? truncate(display, RESULT_MAX) : "";
  };
  const diffData = () => extractDiff(result());

  return (
    <box flexDirection="row" marginTop={1}>
      <box width={2} flexShrink={0}>
        <text fg={theme.text} attributes={textAttributes.muted}>• </text>
      </box>
      <box flexDirection="column" flexGrow={1} minWidth={0}>
        {/* 工具标题本身就是折叠控制，符号同时表达当前展开状态。 */}
        <box onMouseUp={() => setCollapsed((v) => !v)}>
          <text fg={theme.text}>
            <span style={{ fg: theme.suggestion }}>{collapsed() ? "› " : "⌄ "}</span>
            <b>{item().toolName}</b>
            <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
              {argsBrief() ? `  ${argsBrief()}` : ""}{"  "}
            </span>
            <span style={{ fg: statusColor() }}>
              {statusIcon()} {statusText()}
            </span>
          </text>
        </box>

        {/* 展开内容使用树状引导线，避免工具块再形成一张嵌套卡片。 */}
        <Show when={!collapsed()}>
          <box flexDirection="column" minWidth={0}>
            <Show when={argsFull()}>
              <For each={argsFull().split("\n")}>
                {(line, index) => (
                  <text fg={theme.text}>
                    <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
                      {index() === 0 ? "  │ IN   " : "  │      "}
                    </span>
                    {line}
                  </text>
                )}
              </For>
            </Show>

            <Show when={renderResult()}>
              <For each={renderResult().split("\n")}>
                {(line, index) => (
                  <text fg={item().toolError ? theme.error : theme.text}>
                    <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
                      {index() === 0 ? "  │ OUT  " : "  │      "}
                    </span>
                    {line}
                  </text>
                )}
              </For>
            </Show>

            <Show when={diffData()}>
              <For each={diffData()!.text.split("\n").slice(0, 200)}>
                {(line, index) => {
                  const type = classifyLine(line);
                  const fg = type === "add" ? theme.success : type === "remove" ? theme.error : theme.text;
                  return (
                    <text fg={fg} attributes={type === "context" ? textAttributes.muted : undefined}>
                      <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
                        {index() === 0 ? "  └ DIFF " : "         "}
                      </span>
                      {line}
                    </text>
                  );
                }}
              </For>
              <Show when={diffData()!.truncated}>
                <text fg={theme.text} attributes={textAttributes.muted}>
                  ... [diff 已截断，显示 {diffData()!.linesShown}/{diffData()!.linesTotal} 行]
                </text>
              </Show>
            </Show>
          </box>
        </Show>
      </box>
    </box>
  );
}

// ── 纯函数辅助（从旧 ToolBlock.tsx 移植，逻辑不变） ──

function stringifyToolValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function entriesOfArgs(args?: Record<string, unknown>): Array<[string, unknown]> {
  if (!args || typeof args !== "object") return [];
  try {
    return Object.entries(args);
  } catch {
    return [];
  }
}

function summarizeArgs(args?: Record<string, unknown>): string {
  const entries = entriesOfArgs(args);
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

function formatArgsFull(args?: Record<string, unknown>): string {
  const entries = entriesOfArgs(args);
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
  return s.slice(0, max) + `\n... [+${s.length - max} chars truncated, see .cbagent/logs]`;
}

/** 工具结果若是 JSON 且含 __display__/message 字段，提取作为预览，避免整段 JSON 刷屏。 */
function extractDisplay(s: string): string | null {
  const t = s.trimStart();
  if (!t.startsWith("{")) return null;
  try {
    const obj = JSON.parse(s);
    if (obj && typeof obj === "object") {
      if (typeof obj.__display__ === "string") return obj.__display__;
      if (typeof obj.message === "string") return obj.message;
    }
  } catch {
    return null;
  }
  return null;
}

interface DiffData {
  text: string;
  truncated: boolean;
  linesTotal: number;
  linesShown: number;
}

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

function classifyLine(line: string): "header" | "hunk" | "add" | "remove" | "context" {
  if (line.startsWith("--- ") || line.startsWith("+++ ")) return "header";
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "remove";
  return "context";
}
