import { describe, it, expect, beforeEach, afterEach } from "vitest";
import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";
import { HistoryStore } from "../historyStore.js";

describe("HistoryStore", () => {
  let tmpDir: string;
  let file: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "cb-hist-"));
    file = path.join(tmpDir, "history.jsonl");
  });
  afterEach(() => {
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch { /* ignore */ }
  });

  it("文件不存在时 load 不报错且为空", () => {
    const h = new HistoryStore(file, 50);
    h.load();
    expect(h.size()).toBe(0);
    expect(h.all()).toEqual([]);
  });

  it("push 后 all() 按顺序返回", () => {
    const h = new HistoryStore(file, 50);
    h.push("first");
    h.push("second");
    h.push("third");
    expect(h.all()).toEqual(["first", "second", "third"]);
  });

  it("空字符串 / 纯空白被忽略", () => {
    const h = new HistoryStore(file, 50);
    h.push("");
    h.push("   ");
    h.push("\n\t");
    h.push("real");
    expect(h.all()).toEqual(["real"]);
  });

  it("超过 max 后保留最新 N 条", () => {
    const h = new HistoryStore(file, 3);
    h.push("a"); h.push("b"); h.push("c"); h.push("d"); h.push("e");
    expect(h.all()).toEqual(["c", "d", "e"]);
  });

  it("push 落盘后另一个实例 load 能读回（往返）", () => {
    const a = new HistoryStore(file, 50);
    a.push("hello");
    a.push("world");

    const b = new HistoryStore(file, 50);
    b.load();
    expect(b.all()).toEqual(["hello", "world"]);
  });

  it("load 跳过损坏行不崩", () => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(
      file,
      [
        JSON.stringify({ ts: 1, text: "good1" }),
        "{this is broken json",
        JSON.stringify({ ts: 2, text: "good2" }),
        "",
        JSON.stringify({ ts: 3, text: "" }),         // 空文本被过滤
        JSON.stringify({ ts: 4, foo: "no text key" }), // 缺 text 字段
        JSON.stringify({ ts: 5, text: "good3" }),
      ].join("\n") + "\n",
      "utf8",
    );
    const h = new HistoryStore(file, 50);
    h.load();
    expect(h.all()).toEqual(["good1", "good2", "good3"]);
  });

  it("load 后只保留最近 max 条", () => {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    const lines: string[] = [];
    for (let i = 0; i < 10; i++) lines.push(JSON.stringify({ ts: i, text: `t${i}` }));
    fs.writeFileSync(file, lines.join("\n") + "\n", "utf8");

    const h = new HistoryStore(file, 3);
    h.load();
    expect(h.all()).toEqual(["t7", "t8", "t9"]);
  });
});
