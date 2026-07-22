/**
 * ToolBlock：工具调用块。
 *
 * 折叠态：› name  摘要  状态
 * 展开态：
 *   - 普通工具：IN / OUT
 *   - file_edit / file_write：Claude Code 风格
 *       · 标题：path + Added N / Removed M
 *       · 隐藏 old_string / new_string / content / raw JSON
 *       · 使用 OpenTUI 原生 <diff>（行号 + 绿/红着色 unified 视图）
 *
 * 参考：
 *   - Claude Code FileEditToolUpdatedMessage + StructuredDiff
 *   - OpenTUI DiffRenderable（与 opencode TUI 同栈）
 *   - note/文件修改Diff可视化展示技术报告.md
 */

import { createMemo, createSignal, For, Show } from "solid-js";
import { RGBA } from "@opentui/core";
import { useTheme } from "../context/theme.js";
import type { ChatItem } from "../types.js";
import { textAttributes } from "../theme.js";

const RESULT_MAX = 800;
const ARGS_MAX = 400;
const DIFF_VIEW_MAX_LINES = 28;
const FILE_TOOLS = new Set(["file_edit", "file_write"]);

// 行背景：用低饱和索引色，适配终端调色板（避免 truecolor 在浅色主题发灰）
const DIFF_ADDED_BG = RGBA.fromIndex(22); // deep green-ish
const DIFF_REMOVED_BG = RGBA.fromIndex(52); // deep red-ish
const DIFF_CONTEXT_BG = RGBA.defaultBackground();

interface ParsedToolResult {
  ok?: boolean;
  message?: string;
  path?: string;
  type?: string;
  lines_added?: number;
  lines_removed?: number;
  replacements?: number;
  bytes_written?: number;
  error?: string;
  diff?: string;
  diff_truncated?: boolean;
  diff_lines_total?: number;
  diff_lines_shown?: number;
  raw: string;
}

export function ToolBlock(props: { item: ChatItem }) {
  const theme = useTheme();
  const item = () => props.item;
  // 初始折叠态以 store 为准；file_edit/file_write 在 tool_start 已写 collapsed:false
  const [collapsed, setCollapsed] = createSignal(
    props.item.collapsed ?? !FILE_TOOLS.has(props.item.toolName || ""),
  );

  const toolName = () => item().toolName || "unknown";
  const isFileTool = () => FILE_TOOLS.has(toolName());
  const parsed = createMemo(() => parseToolResult(item().toolResult));

  const statusColor = () =>
    !item().toolDone ? theme.info : item().toolError ? theme.error : theme.success;
  const statusIcon = () =>
    !item().toolDone ? "○" : item().toolError ? "✗" : "✓";
  const statusText = () =>
    item().toolDone ? `${item().toolDuration?.toFixed(2) ?? "?"}s` : "running";

  /** 折叠标题：file 工具 → path + Added/Removed；其它 → 参数摘要 */
  const titleBrief = createMemo(() => {
    if (isFileTool()) {
      const p = parsed();
      const path = shortPath(p.path || stringArg(item().toolArgs, "path") || "");
      const stats = formatClaudeStyleStats(p);
      return [path, stats].filter(Boolean).join("  ");
    }
    return summarizeArgs(item().toolArgs);
  });

  /** 展开摘要行（Claude：Added N lines, removed M lines） */
  const summaryLine = createMemo(() => {
    if (!isFileTool()) return "";
    return formatClaudeStyleStats(parsed(), true);
  });

  const argsLines = createMemo(() => {
    if (isFileTool()) {
      // 有 diff 时几乎不展示 IN；仅在无 diff 时给 path 兜底
      if (parsed().diff) return [] as string[];
      return formatFileToolArgs(item().toolArgs, parsed());
    }
    const f = formatArgsFull(item().toolArgs);
    if (!f) return [] as string[];
    return truncate(f, ARGS_MAX).split("\n");
  });

  const outLines = createMemo(() => {
    const p = parsed();
    if (item().toolError) {
      const err = p.error || p.message || p.raw;
      return truncate(err, RESULT_MAX).split("\n");
    }
    if (isFileTool()) {
      // 有 diff 时 OUT 不再重复 message（已在摘要行）
      if (p.diff) return [] as string[];
      if (p.message) return [p.message];
      return [] as string[];
    }
    const display = p.message || extractDisplay(p.raw) || p.raw;
    if (!display) return [] as string[];
    return truncate(display, RESULT_MAX).split("\n");
  });

  const fileDiff = createMemo(() => {
    if (!isFileTool()) return null as DiffViewModel | null;
    const raw = parsed().diff || "";
    if (!raw.trim()) return null;
    return prepareDiffForView(raw);
  });

  /** Diff 组件可视高度：行数上限，避免超大 patch 撑爆视口 */
  const diffHeight = createMemo(() => {
    const d = fileDiff();
    if (!d || !d.ok) return 4;
    const p = parsed();
    const total = p.diff_lines_shown || countLines(d.text) || 1;
    return Math.max(3, Math.min(DIFF_VIEW_MAX_LINES, total + 1));
  });

  const filetype = createMemo(() => guessFiletype(parsed().path || stringArg(item().toolArgs, "path") || ""));

  return (
    <box flexDirection="row" marginTop={1}>
      <box width={2} flexShrink={0}>
        <text fg={theme.text} attributes={textAttributes.muted}>• </text>
      </box>
      <box flexDirection="column" flexGrow={1} minWidth={0}>
        <box onMouseUp={() => setCollapsed((v) => !v)}>
          <text fg={theme.text} wrapMode="none">
            <span style={{ fg: theme.suggestion }}>{collapsed() ? "› " : "⌄ "}</span>
            <b>{toolName()}</b>
            <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
              {titleBrief() ? `  ${titleBrief()}` : ""}{"  "}
            </span>
            <span style={{ fg: statusColor() }}>
              {statusIcon()} {statusText()}
            </span>
          </text>
        </box>

        <Show when={!collapsed()}>
          <box flexDirection="column" minWidth={0}>
            {/* Claude 风格：先一行 Added/Removed，再原生 Diff */}
            <Show when={isFileTool() && summaryLine()}>
              <text fg={theme.text}>
                <span style={{ fg: theme.text, attributes: textAttributes.muted }}>{"  │ "}</span>
                {summaryLine()}
              </text>
            </Show>

            <Show when={argsLines().length > 0}>
              <For each={argsLines()}>
                {(line, index) => (
                  <text fg={theme.text} wrapMode="word">
                    <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
                      {index() === 0 ? "  │ IN   " : "  │      "}
                    </span>
                    {line}
                  </text>
                )}
              </For>
            </Show>

            <Show when={outLines().length > 0}>
              <For each={outLines()}>
                {(line, index) => (
                  <text
                    fg={item().toolError ? theme.error : theme.text}
                    wrapMode="word"
                  >
                    <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
                      {index() === 0 ? "  │ OUT  " : "  │      "}
                    </span>
                    {line}
                  </text>
                )}
              </For>
            </Show>

            <Show when={fileDiff()}>
              {(d) => {
                const model = d();
                if (!model.ok) {
                  return (
                    <box flexDirection="column" minWidth={0} marginTop={0} paddingLeft={2}>
                      <text fg={theme.error} wrapMode="word">
                        {`  │ Diff 无法解析：${model.error}`}
                      </text>
                      <text fg={theme.text} attributes={textAttributes.muted} wrapMode="word">
                        {"  │ 常见原因：patch 行缺少 +/-/空格 前缀，或截断切断了 hunk 中部。"}
                      </text>
                      <For each={model.previewLines.slice(0, 8)}>
                        {(line) => (
                          <text fg={theme.text} attributes={textAttributes.muted} wrapMode="none">
                            {`  │ ${line}`}
                          </text>
                        )}
                      </For>
                    </box>
                  );
                }
                return (
                  <box flexDirection="column" minWidth={0} marginTop={0} paddingLeft={2}>
                    <diff
                      diff={model.text}
                      view="unified"
                      showLineNumbers={true}
                      wrapMode="word"
                      filetype={filetype()}
                      height={diffHeight()}
                      fg={theme.text}
                      addedSignColor={theme.success}
                      removedSignColor={theme.error}
                      addedBg={DIFF_ADDED_BG}
                      removedBg={DIFF_REMOVED_BG}
                      contextBg={DIFF_CONTEXT_BG}
                      addedContentBg={DIFF_ADDED_BG}
                      removedContentBg={DIFF_REMOVED_BG}
                      contextContentBg={DIFF_CONTEXT_BG}
                      lineNumberFg={theme.textMuted}
                      lineNumberBg={DIFF_CONTEXT_BG}
                      addedLineNumberBg={DIFF_ADDED_BG}
                      removedLineNumberBg={DIFF_REMOVED_BG}
                    />
                    <Show when={parsed().diff_truncated || (parsed().diff_lines_total ?? 0) > DIFF_VIEW_MAX_LINES}>
                      <text fg={theme.text} attributes={textAttributes.muted}>
                        {`  … [diff 已截断`}
                        {parsed().diff_lines_total
                          ? `，约 ${Math.min(DIFF_VIEW_MAX_LINES, parsed().diff_lines_shown ?? DIFF_VIEW_MAX_LINES)}/${parsed().diff_lines_total} 行`
                          : ""}
                        {"]"}
                      </text>
                    </Show>
                  </box>
                );
              }}
            </Show>
          </box>
        </Show>
      </box>
    </box>
  );
}

// ── 解析 / 摘要 ──

function parseToolResult(raw: unknown): ParsedToolResult {
  const text = stringifyToolValue(raw);
  const base: ParsedToolResult = { raw: text };
  const t = text.trimStart();
  if (!t.startsWith("{")) return base;
  try {
    const obj = JSON.parse(text);
    if (!obj || typeof obj !== "object") return base;
    return {
      ...base,
      ok: typeof obj.ok === "boolean" ? obj.ok : undefined,
      message: typeof obj.message === "string" ? obj.message : undefined,
      path: typeof obj.path === "string" ? obj.path : undefined,
      type: typeof obj.type === "string" ? obj.type : undefined,
      lines_added: typeof obj.lines_added === "number" ? obj.lines_added : undefined,
      lines_removed: typeof obj.lines_removed === "number" ? obj.lines_removed : undefined,
      replacements: typeof obj.replacements === "number" ? obj.replacements : undefined,
      bytes_written: typeof obj.bytes_written === "number" ? obj.bytes_written : undefined,
      error: typeof obj.error === "string" ? obj.error : undefined,
      diff: typeof obj.diff === "string" ? obj.diff : undefined,
      diff_truncated: !!obj.diff_truncated,
      diff_lines_total: typeof obj.diff_lines_total === "number" ? obj.diff_lines_total : undefined,
      diff_lines_shown: typeof obj.diff_lines_shown === "number" ? obj.diff_lines_shown : undefined,
    };
  } catch {
    return base;
  }
}

/** Claude Code 风格：Added N lines, removed M lines */
function formatClaudeStyleStats(p: ParsedToolResult, verbose = false): string {
  const add = p.lines_added ?? 0;
  const rem = p.lines_removed ?? 0;
  const parts: string[] = [];

  if (p.type === "create") {
    if (verbose) parts.push(add > 0 ? `Created · Added ${add} ${add === 1 ? "line" : "lines"}` : "Created");
    else parts.push(add > 0 ? `create +${add}` : "create");
  } else if (verbose) {
    if (add > 0) parts.push(`Added ${add} ${add === 1 ? "line" : "lines"}`);
    if (rem > 0) {
      const word = add > 0 ? "removed" : "Removed";
      parts.push(`${word} ${rem} ${rem === 1 ? "line" : "lines"}`);
    }
    if (typeof p.replacements === "number" && p.replacements > 0) {
      parts.push(`${p.replacements} replacement${p.replacements === 1 ? "" : "s"}`);
    }
    if (parts.length === 0 && p.message) return p.message;
  } else {
    if (add || rem) parts.push(`+${add}/-${rem}`);
    if (typeof p.replacements === "number" && p.replacements > 0) {
      parts.push(`${p.replacements} rep`);
    }
  }
  return parts.join(verbose ? ", " : " ");
}

function formatFileToolArgs(
  args: Record<string, unknown> | undefined,
  parsed: ParsedToolResult,
): string[] {
  const lines: string[] = [];
  const path = stringArg(args, "path") || parsed.path;
  if (path) lines.push(`path=${path}`);
  if (args && typeof args.replace_all === "boolean") {
    lines.push(`replace_all=${args.replace_all}`);
  }
  const oldS = stringArg(args, "old_string");
  const newS = stringArg(args, "new_string");
  const content = stringArg(args, "content");
  if (oldS !== undefined) lines.push(`old_string: ${oldS.length} chars`);
  if (newS !== undefined) lines.push(`new_string: ${newS.length} chars`);
  if (content !== undefined) lines.push(`content: ${content.length} chars`);
  return lines;
}

function shortPath(path: string): string {
  if (!path) return "";
  const parts = path.replace(/\\/g, "/").split("/");
  if (parts.length <= 3) return path;
  return `…/${parts.slice(-2).join("/")}`;
}

function stringArg(args: Record<string, unknown> | undefined, key: string): string | undefined {
  if (!args) return undefined;
  const v = args[key];
  return typeof v === "string" ? v : undefined;
}

function countLines(s: string): number {
  if (!s) return 0;
  let n = 1;
  for (let i = 0; i < s.length; i++) if (s.charCodeAt(i) === 10) n++;
  return n;
}

/** OpenTUI <diff> / jsdiff 可渲染的视图模型 */
type DiffViewModel =
  | { ok: true; text: string }
  | { ok: false; text: string; error: string; previewLines: string[] };

/**
 * 清洗 + 轻量校验 unified patch，避免 OpenTUI 抛
 * `Error parsing diff: Hunk at line N contained invalid line ...`
 * 却只显示这句含糊错误。
 */
function prepareDiffForView(raw: string): DiffViewModel {
  const normalized = raw.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  // 去掉 JSON 转义残留的多余空行，保证 ---/+++/@@ 后是合法 hunk 体
  const lines = normalized.split("\n");
  // 丢弃末位空串（分裂自末尾 \n），避免空行被当 hunk 体空格行多塞一行
  while (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
  const cleaned: string[] = [];
  let inHunk = false;
  let bad: { lineNo: number; line: string; reason: string } | null = null;

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineNo = i + 1;

    if (line.startsWith("--- ") || line.startsWith("+++ ") || line.startsWith("diff ")) {
      inHunk = false;
      cleaned.push(line);
      continue;
    }
    if (line.startsWith("@@")) {
      inHunk = true;
      cleaned.push(line);
      continue;
    }
    // 文件元数据（index / similarity 等）可忽略
    if (!inHunk) {
      if (line === "" && cleaned.length > 0) continue;
      cleaned.push(line);
      continue;
    }

    // hunk 体内：允许空行（当上下文空行）→ 补成 " " 前缀
    if (line === "") {
      cleaned.push(" ");
      continue;
    }
    const op = line[0];
    if (op === "+" || op === "-" || op === " " || op === "\\") {
      cleaned.push(line);
      continue;
    }
    // 粘连的非法行（如 "-old+new" 中第二段以 + 开头但整行以 - 起也可 parse；
    // 真正非法是不以这四类开头，例如 "@@" 中途坏了或 "Index:" 混入）
    // 若整行像被粘成 "-foo+bar" 仍以 - 开头，jsdiff 会当删除行接受；
    // 真正炸的是类似 "line without prefix" 或截断后残片。
    bad = {
      lineNo,
      line: line.length > 120 ? line.slice(0, 120) + "…" : line,
      reason: `hunk 体第 ${lineNo} 行缺少 +/-/空格/\\\\ 前缀`,
    };
    break;
  }

  const text = cleaned.join("\n").replace(/\n+$/, "\n");
  if (bad) {
    return {
      ok: false,
      text,
      error: `${bad.reason}：${JSON.stringify(bad.line)}`,
      previewLines: lines.slice(Math.max(0, bad.lineNo - 3), bad.lineNo + 2).map((l, idx) => {
        const n = Math.max(0, bad!.lineNo - 3) + idx + 1;
        return `${n}| ${l}`;
      }),
    };
  }
  if (!text.includes("@@")) {
    return {
      ok: false,
      text,
      error: "缺少 @@ hunk 头（不是完整 unified diff）",
      previewLines: lines.slice(0, 8).map((l, i) => `${i + 1}| ${l}`),
    };
  }
  return { ok: true, text };
}

function guessFiletype(path: string): string | undefined {
  const lower = path.toLowerCase();
  const m = lower.match(/\.([a-z0-9]+)$/);
  if (!m) return undefined;
  const ext = m[1];
  const map: Record<string, string> = {
    ts: "typescript",
    tsx: "tsx",
    js: "javascript",
    jsx: "jsx",
    py: "python",
    rs: "rust",
    go: "go",
    md: "markdown",
    json: "json",
    yaml: "yaml",
    yml: "yaml",
    css: "css",
    html: "html",
    sh: "bash",
    bash: "bash",
    zsh: "bash",
    toml: "toml",
    c: "c",
    h: "c",
    cpp: "cpp",
    java: "java",
  };
  return map[ext] ?? ext;
}

// ── 通用辅助 ──

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
  return s.slice(0, max) + `\n... [+${s.length - max} chars truncated]`;
}

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
