import { describe, it, expect } from "vitest";
import { parseBlocks, parseInline, visibleWidth } from "../components/Markdown.js";

describe("markdown parseBlocks", () => {
  it("解析标题", () => {
    const bs = parseBlocks("# H1\n## H2");
    expect(bs).toHaveLength(2);
    expect(bs[0]).toMatchObject({ kind: "heading", level: 1, text: "H1" });
    expect(bs[1]).toMatchObject({ kind: "heading", level: 2, text: "H2" });
  });

  it("解析无序列表", () => {
    const bs = parseBlocks("- a\n- b\n- c");
    expect(bs).toHaveLength(1);
    expect(bs[0]).toMatchObject({ kind: "ul", items: ["a", "b", "c"] });
  });

  it("解析有序列表", () => {
    const bs = parseBlocks("1. a\n2. b");
    expect(bs[0]).toMatchObject({ kind: "ol", items: ["a", "b"] });
  });

  it("解析围栏代码块", () => {
    const bs = parseBlocks("```py\nprint(1)\nprint(2)\n```");
    expect(bs[0]).toMatchObject({ kind: "code", lang: "py", lines: ["print(1)", "print(2)"] });
  });

  it("解析 GFM 表格 + 对齐", () => {
    const bs = parseBlocks("| a | b | c |\n|:--|:--:|--:|\n| 1 | 2 | 3 |\n| 4 | 5 | 6 |");
    expect(bs).toHaveLength(1);
    expect(bs[0]).toMatchObject({
      kind: "table",
      header: ["a", "b", "c"],
      aligns: ["left", "center", "right"],
      rows: [["1", "2", "3"], ["4", "5", "6"]],
    });
  });

  it("中文表格按宽字符算列宽", () => {
    expect(visibleWidth("语言")).toBe(4);   // 2 个 CJK = 4
    expect(visibleWidth("Python")).toBe(6);
    expect(visibleWidth("解释型")).toBe(6); // 3 个 CJK = 6
  });

  it("水平线", () => {
    const bs = parseBlocks("a\n\n---\n\nb");
    expect(bs.map((b) => b.kind)).toContain("hr");
  });

  it("段落合并连续行", () => {
    const bs = parseBlocks("hello\nworld");
    expect(bs).toHaveLength(1);
    expect(bs[0]).toMatchObject({ kind: "para", text: "hello world" });
  });
});

describe("markdown parseInline", () => {
  it("**bold**", () => {
    expect(parseInline("a **b** c")).toEqual([
      { text: "a " },
      { text: "b", bold: true },
      { text: " c" },
    ]);
  });

  it("`code`", () => {
    expect(parseInline("x `y` z")).toEqual([
      { text: "x " },
      { text: "y", code: true },
      { text: " z" },
    ]);
  });

  it("[text](url) 只保留 text", () => {
    expect(parseInline("see [docs](https://x.com) ok")).toEqual([
      { text: "see " },
      { text: "docs" },
      { text: " ok" },
    ]);
  });

  it("纯文本", () => {
    expect(parseInline("hello")).toEqual([{ text: "hello" }]);
  });
});
