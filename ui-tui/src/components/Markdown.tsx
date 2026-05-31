/**
 * 轻量 Markdown 渲染器（ink 版）。
 *
 * 终端真正用得上的 md 子集：标题、段落（行内 **bold** / *em* / `code` / [text](url)）、
 * 无序/有序列表、围栏代码块、GFM 表格、水平线。
 *
 * 不引 marked / react-markdown：前者出 HTML AST 还得二次翻译，后者吃 DOM、ink 上水土
 * 不服。自己一行一行扫 + 状态机切块，覆盖以上就够用。
 *
 * 表格宽度计算：CJK 一个字符按 2 算，ASCII 按 1，避免中文表格列宽撑不开。
 */

import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";

type Align = "left" | "center" | "right";

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "para"; text: string }
  | { kind: "code"; lang: string; lines: string[] }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][]; aligns: Align[] }
  | { kind: "hr" }
  | { kind: "blank" };

interface Span { text: string; bold?: boolean; italic?: boolean; code?: boolean }

export function Markdown({ text }: { text: string }) {
  const blocks = parseBlocks(text);
  return (
    <Box flexDirection="column">
      {blocks.map((b, i) => <BlockView key={i} b={b} />)}
    </Box>
  );
}

// ---------- 解析 ----------

export function parseBlocks(text: string): Block[] {
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^```(\w*)\s*$/.exec(line);
    if (fence) {
      const buf: string[] = [];
      i += 1;
      while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      if (i < lines.length) i += 1;
      out.push({ kind: "code", lang: fence[1] ?? "", lines: buf });
      continue;
    }
    if (/^\s*(-{3,}|_{3,}|\*{3,})\s*$/.test(line)) { out.push({ kind: "hr" }); i += 1; continue; }
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const header = splitRow(line);
      const aligns = parseAligns(lines[i + 1]);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        rows.push(splitRow(lines[i])); i += 1;
      }
      out.push({ kind: "table", header, rows, aligns });
      continue;
    }
    const h = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (h) { out.push({ kind: "heading", level: h[1].length, text: h[2] }); i += 1; continue; }
    if (/^\s*[-*+]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*+]\s+/, "")); i += 1;
      }
      out.push({ kind: "ul", items });
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i += 1;
      }
      out.push({ kind: "ol", items });
      continue;
    }
    if (line.trim() === "") {
      while (i < lines.length && lines[i].trim() === "") i += 1;
      out.push({ kind: "blank" });
      continue;
    }
    const buf: string[] = [];
    while (
      i < lines.length && lines[i].trim() !== "" &&
      !/^#{1,6}\s+/.test(lines[i]) && !/^```/.test(lines[i]) &&
      !/^\s*[-*+]\s+/.test(lines[i]) && !/^\s*\d+\.\s+/.test(lines[i]) &&
      !/^\s*(-{3,}|_{3,}|\*{3,})\s*$/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))
    ) { buf.push(lines[i]); i += 1; }
    if (buf.length > 0) out.push({ kind: "para", text: buf.join(" ") });
  }
  return out;
}

function isTableSep(line: string): boolean {
  // 至少 2 dash 即可（GFM 标准是 3+，但 :--: 也常见，兼容）
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$/.test(line);
}
function parseAligns(sep: string): Align[] {
  return splitRow(sep).map((c) => {
    const t = c.trim();
    const l = t.startsWith(":"), r = t.endsWith(":");
    return l && r ? "center" : r ? "right" : "left";
  });
}
function splitRow(line: string): string[] {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

// ---------- 行内 ----------

export function parseInline(text: string): Span[] {
  const spans: Span[] = [];
  const re = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[([^\]]+)\]\([^)]+\))/g;
  let last = 0; let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) spans.push({ text: text.slice(last, m.index) });
    if (m[1]) spans.push({ text: m[1].slice(1, -1), code: true });
    else if (m[2]) spans.push({ text: m[2].slice(2, -2), bold: true });
    else if (m[3]) spans.push({ text: m[3].slice(1, -1), italic: true });
    else if (m[4]) spans.push({ text: m[5] });
    last = re.lastIndex;
  }
  if (last < text.length) spans.push({ text: text.slice(last) });
  return spans;
}

function Inline({ text }: { text: string }) {
  const spans = parseInline(text);
  return (
    <Text>
      {spans.map((s, i) => (
        <Text
          key={i}
          bold={s.bold}
          italic={s.italic}
          color={s.code ? theme.warning : undefined}
          backgroundColor={s.code ? theme.bashBorder : undefined}
        >{s.text}</Text>
      ))}
    </Text>
  );
}

// ---------- 块渲染 ----------

function BlockView({ b }: { b: Block }) {
  if (b.kind === "blank") return <Box height={1} />;
  if (b.kind === "hr") return <Text dimColor>{"─".repeat(40)}</Text>;
  if (b.kind === "heading") {
    const colors = [theme.primary, theme.accent, theme.suggestion, theme.info, theme.info, theme.info];
    const prefix = "#".repeat(b.level) + " ";
    return (
      <Text bold color={colors[b.level - 1]}>
        {prefix}<Inline text={b.text} />
      </Text>
    );
  }
  if (b.kind === "para") return <Inline text={b.text} />;
  if (b.kind === "code") {
    return (
      <Box flexDirection="column" borderStyle="single" borderColor={theme.bashBorder} paddingX={1}>
        {b.lang && <Text dimColor>{b.lang}</Text>}
        {b.lines.map((l, i) => <Text key={i} color={theme.warning}>{l || " "}</Text>)}
      </Box>
    );
  }
  if (b.kind === "ul") {
    return (
      <Box flexDirection="column">
        {b.items.map((it, i) => (
          <Box key={i}>
            <Text color={theme.suggestion}>• </Text>
            <Inline text={it} />
          </Box>
        ))}
      </Box>
    );
  }
  if (b.kind === "ol") {
    return (
      <Box flexDirection="column">
        {b.items.map((it, i) => (
          <Box key={i}>
            <Text color={theme.suggestion}>{i + 1}. </Text>
            <Inline text={it} />
          </Box>
        ))}
      </Box>
    );
  }
  if (b.kind === "table") return <Table header={b.header} rows={b.rows} aligns={b.aligns} />;
  return null;
}

// ---------- 表格 ----------

export function visibleWidth(s: string): number {
  // 行内格式不影响列宽（** 等字面被剥掉算）
  const plain = s.replace(/\*\*([^*]+)\*\*/g, "$1").replace(/\*([^*]+)\*/g, "$1")
    .replace(/`([^`]+)`/g, "$1").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  let w = 0;
  for (const ch of plain) {
    const cp = ch.codePointAt(0) ?? 0;
    // CJK 主要区段：CJK 统一汉字 / 全角符号 / 假名 / 韩文 等，全部按 2 列宽
    const wide =
      (cp >= 0x1100 && cp <= 0x115F) ||
      (cp >= 0x2E80 && cp <= 0x303E) ||
      (cp >= 0x3041 && cp <= 0x33FF) ||
      (cp >= 0x3400 && cp <= 0x4DBF) ||
      (cp >= 0x4E00 && cp <= 0x9FFF) ||
      (cp >= 0xA000 && cp <= 0xA4CF) ||
      (cp >= 0xAC00 && cp <= 0xD7A3) ||
      (cp >= 0xF900 && cp <= 0xFAFF) ||
      (cp >= 0xFE30 && cp <= 0xFE4F) ||
      (cp >= 0xFF00 && cp <= 0xFF60) ||
      (cp >= 0xFFE0 && cp <= 0xFFE6);
    w += wide ? 2 : 1;
  }
  return w;
}

function pad(s: string, width: number, align: Align): string {
  const slack = width - visibleWidth(s);
  if (slack <= 0) return s;
  if (align === "right") return " ".repeat(slack) + s;
  if (align === "center") {
    const l = Math.floor(slack / 2);
    return " ".repeat(l) + s + " ".repeat(slack - l);
  }
  return s + " ".repeat(slack);
}

function Table({ header, rows, aligns }: { header: string[]; rows: string[][]; aligns: Align[] }) {
  const cols = header.length;
  const widths: number[] = [];
  for (let c = 0; c < cols; c++) {
    let w = visibleWidth(header[c] ?? "");
    for (const r of rows) w = Math.max(w, visibleWidth(r[c] ?? ""));
    widths.push(w);
  }
  const sep = "+" + widths.map((w) => "-".repeat(w + 2)).join("+") + "+";
  const renderRow = (cells: string[], bold: boolean) => (
    <Box>
      <Text dimColor>|</Text>
      {cells.slice(0, cols).map((cell, i) => (
        <React.Fragment key={i}>
          <Text> </Text>
          <Text bold={bold}><Inline text={pad(cell ?? "", widths[i], aligns[i] ?? "left")} /></Text>
          <Text> </Text>
          <Text dimColor>|</Text>
        </React.Fragment>
      ))}
    </Box>
  );
  return (
    <Box flexDirection="column">
      <Text dimColor>{sep}</Text>
      {renderRow(header, true)}
      <Text dimColor>{sep}</Text>
      {rows.map((r, i) => <React.Fragment key={i}>{renderRow(r, false)}</React.Fragment>)}
      <Text dimColor>{sep}</Text>
    </Box>
  );
}
