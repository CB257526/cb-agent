/**
 * 命令历史存储。
 *
 * 落盘到 ~/.cb-agent/history.jsonl，每行一条 JSON {ts, text}。
 * 内存里维护一个 ring buffer（默认 200 条上限），App 启动时读最后 N 条进内存，
 * push() 同步写文件 append（O_APPEND，不锁）。
 *
 * 设计取舍：
 *   - 不存斜杠命令（push() 调用方决定）—— 历史用于翻找真实 prompt
 *   - 不做去重 —— 用户可能有意重复同一个 prompt 试错
 *   - 不做加密 —— prompt 本身可能含敏感词，但写到 ~/.cb-agent 已是用户私有目录
 *   - 跨进程并发 append 安全（POSIX/Win32 O_APPEND 都原子），但读时可能看到最近一条只写了一半 —
 *     try/catch 跳过解析失败的行
 */

import * as fs from "node:fs";
import * as os from "node:os";
import * as path from "node:path";

const DEFAULT_MAX = 200;
const DEFAULT_FILE = path.join(os.homedir(), ".cb-agent", "history.jsonl");

export class HistoryStore {
  private items: string[] = [];

  constructor(
    private readonly file: string = DEFAULT_FILE,
    private readonly max: number = DEFAULT_MAX,
  ) {}

  /** 同步加载历史（启动时调一次即可）。文件不存在不报错。 */
  load(): void {
    if (!fs.existsSync(this.file)) {
      this.items = [];
      return;
    }
    try {
      const raw = fs.readFileSync(this.file, "utf8");
      const lines = raw.split(/\r?\n/).filter(Boolean);
      const parsed: string[] = [];
      for (const line of lines) {
        try {
          const obj = JSON.parse(line);
          if (typeof obj?.text === "string" && obj.text.trim()) {
            parsed.push(obj.text);
          }
        } catch {
          // 半行 JSON、损坏行，跳过
        }
      }
      this.items = parsed.slice(-this.max);
    } catch {
      this.items = [];
    }
  }

  /** 追加一条；自动落盘。空字符串/纯空白被忽略。 */
  push(text: string): void {
    const trimmed = text.trim();
    if (!trimmed) return;
    this.items.push(trimmed);
    if (this.items.length > this.max) {
      this.items = this.items.slice(-this.max);
    }
    try {
      fs.mkdirSync(path.dirname(this.file), { recursive: true });
      fs.appendFileSync(
        this.file,
        JSON.stringify({ ts: Date.now(), text: trimmed }) + "\n",
        "utf8",
      );
    } catch {
      // 写盘失败不影响内存里这条；下次启动可能丢，能接受
    }
  }

  /** 取所有历史（旧→新）。 */
  all(): readonly string[] {
    return this.items;
  }

  /** 仅测试用 */
  size(): number {
    return this.items.length;
  }
}
