/**
 * cb-agent stdio JSON-RPC 客户端。
 *
 * 职责：
 *   1. spawn `python run_agent.py --transport jsonrpc`，把 stdout 当事件流读
 *   2. 行缓冲解析 NDJSON（一行一条 JSON object，可能被 OS pipe 切成多个 chunk）
 *   3. 区分 notification（事件，无 id）和 response（带 id），分别派发
 *   4. 提供 sendPrompt / cancel / quit 三个方法（封装 RPC id 生成）
 *
 * 不做的事：
 *   - 重连：Python 端崩了直接退 UI，让用户重启
 *   - rpc 超时：cb-agent 不需要长 RPC，prompt.submit 立即响应、其它都是同步
 *   - 消息队列：response 里的 result 不被业务用，UI 只关心事件流
 *
 * stderr 单独导向日志文件，避免 Python 启动期诊断输出污染 UI 屏幕。
 */

import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { EventEmitter } from "node:events";
import { createWriteStream, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { AgentEvent, CompactPayload, MCPStatusPayload, SessionPayload, SessionSummary } from "./types.js";

export const RUN_AGENT_ARGS = ["run_agent.py", "--transport", "jsonrpc", "--memory-system", "light"];
export const STDERR_UI_LINE_MAX = 4000;

function clipStderrForUi(line: string): string {
  if (line.length <= STDERR_UI_LINE_MAX) return line;
  const omitted = line.length - STDERR_UI_LINE_MAX;
  return `${line.slice(0, STDERR_UI_LINE_MAX)}…（实时日志已截断 ${omitted} 字符，完整内容见日志文件）`;
}

export interface TransportOptions {
  /** Python 解释器路径。默认环境变量 CB_AGENT_PYTHON 或 "python"。 */
  python?: string;
  /** run_agent.py 所在的工作目录。默认 ../（ui-tui 的父目录） */
  cwd?: string;
  /** 额外环境变量 */
  env?: NodeJS.ProcessEnv;
  /** stderr 日志路径。默认 ~/.cb-agent/logs/gateway-<ts>.log */
  stderrLog?: string;
}

export interface TransportEvents {
  event: (ev: AgentEvent) => void;
  response: (id: string | number, body: { result?: unknown; error?: { code: number; message: string } }) => void;
  exit: (code: number | null) => void;
  /** 协议解析失败、stdout 出现非 JSON 行——通常是 Python 端漏了 stdout 重定向 */
  protocolError: (raw: string, err: Error) => void;
  /** Python 端的 stderr 一行（不含末尾换行）。同时还在写日志文件，事件只是给 UI 实时面板用。 */
  stderr: (line: string) => void;
}

export declare interface Transport {
  on<K extends keyof TransportEvents>(event: K, listener: TransportEvents[K]): this;
  emit<K extends keyof TransportEvents>(event: K, ...args: Parameters<TransportEvents[K]>): boolean;
}

export class Transport extends EventEmitter {
  private proc: ChildProcessWithoutNullStreams;
  private stdoutBuf = "";
  private stderrBuf = "";
  private rpcCounter = 0;
  private stderrLogPath: string;

  constructor(opts: TransportOptions = {}) {
    super();
    const python = opts.python ?? process.env.CB_AGENT_PYTHON ?? "python";
    const cwd = opts.cwd ?? join(process.cwd(), "..");

    // 解析 stderr 日志路径
    const logsDir = join(homedir(), ".cb-agent", "logs");
    mkdirSync(logsDir, { recursive: true });
    this.stderrLogPath = opts.stderrLog ?? join(logsDir, `gateway-${Date.now()}.log`);

    this.proc = spawn(python, RUN_AGENT_ARGS, {
      cwd,
      env: { ...process.env, ...opts.env, PYTHONIOENCODING: "utf-8" },
      stdio: ["pipe", "pipe", "pipe"],
    });

    // stdout: NDJSON 协议
    this.proc.stdout.setEncoding("utf-8");
    this.proc.stdout.on("data", (chunk: string) => this.handleStdout(chunk));

    // stderr: 同时做两件事
    //   1. 全量写日志文件（保留最完整记录，事故归档）
    //   2. 行缓冲解析后 emit stderr 事件（UI 实时面板用，行内不带换行）
    const logStream = createWriteStream(this.stderrLogPath, { flags: "a" });
    this.proc.stderr.setEncoding("utf-8");
    this.proc.stderr.on("data", (chunk: string) => {
      logStream.write(chunk);
      this.handleStderr(chunk);
    });

    this.proc.on("exit", (code) => this.emit("exit", code));
    this.proc.on("error", (err) => {
      this.emit("protocolError", "spawn-failed", err);
    });
  }

  private handleStdout(chunk: string): void {
    this.stdoutBuf += chunk;
    let nl: number;
    while ((nl = this.stdoutBuf.indexOf("\n")) !== -1) {
      const line = this.stdoutBuf.slice(0, nl).trim();
      this.stdoutBuf = this.stdoutBuf.slice(nl + 1);
      if (!line) continue;
      this.handleLine(line);
    }
  }

  private handleStderr(chunk: string): void {
    this.stderrBuf += chunk;
    let nl: number;
    while ((nl = this.stderrBuf.indexOf("\n")) !== -1) {
      // 保留原文本（不 trim 内容空格，只去末尾 \r），空行也 emit 让面板视觉间距正确
      let line = this.stderrBuf.slice(0, nl);
      this.stderrBuf = this.stderrBuf.slice(nl + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);
      // stderr 原文已经在调用 handleStderr 前写入日志文件；这里 emit 给 TUI 的只是
      // 实时面板预览。超长 messages dump / JSON 行如果完整进入 React state，会让
      // Ink 反复重绘大字符串，表现为终端卡死和 VS Code 内存飙升，所以只截断 UI 副本。
      this.emit("stderr", clipStderrForUi(line));
    }
  }

  private handleLine(line: string): void {
    let msg: any;
    try {
      msg = JSON.parse(line);
    } catch (e) {
      this.emit("protocolError", line, e as Error);
      return;
    }
    if (msg && msg.method === "event" && msg.params) {
      this.emit("event", msg.params as AgentEvent);
    } else if (msg && (msg.id !== undefined && msg.id !== null)) {
      this.emit("response", msg.id, { result: msg.result, error: msg.error });
    }
    // 其它消息（notification 但非 event）静默丢弃
  }

  /** 发送一条 JSON-RPC 请求。不等响应。 */
  private sendRpc(method: string, params: Record<string, unknown> = {}): string {
    const id = `r${++this.rpcCounter}`;
    const msg = JSON.stringify({ jsonrpc: "2.0", id, method, params }) + "\n";
    this.proc.stdin.write(msg);
    return id;
  }

  /** 发送 RPC 并等响应（5s 超时）。仅用于 list_tools 这种需要数据回来的场景。 */
  private requestRpc<T = unknown>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    const id = this.sendRpc(method, params);
    return new Promise<T>((resolve, reject) => {
      const onResp = (rid: string | number, body: { result?: unknown; error?: { code: number; message: string } }) => {
        if (rid !== id) return;
        this.removeListener("response", onResp);
        clearTimeout(timer);
        if (body.error) reject(new Error(body.error.message));
        else resolve(body.result as T);
      };
      const timer = setTimeout(() => {
        this.removeListener("response", onResp);
        reject(new Error(`RPC ${method} timeout`));
      }, 5000);
      this.on("response", onResp);
    });
  }

  sendPrompt(text: string): string {
    return this.sendRpc("prompt.submit", { text });
  }

  cancel(): string {
    return this.sendRpc("session.cancel");
  }

  clearHistory(): string {
    return this.sendRpc("session.clear_history");
  }

  /** 压缩当前 active 会话上下文；不重绘 UI，只返回压缩结果供命令提示。 */
  compactSession(): Promise<CompactPayload> {
    return this.requestRpc("session.compact");
  }

  /** 列出项目级本地会话摘要。只返回短 preview，不返回 transcript 全文。 */
  listSessions(): Promise<{ sessions: SessionSummary[]; current?: SessionSummary | null }> {
    return this.requestRpc("session.list_sessions");
  }

  /** 新建空白会话并切换过去；后端会返回空 history 供 UI 重绘。 */
  createSession(): Promise<SessionPayload> {
    return this.requestRpc("session.create");
  }

  /** 切换到已有会话；后端返回该会话恢复后的普通 history。 */
  switchSession(session_id: string): Promise<SessionPayload> {
    return this.requestRpc("session.switch", { session_id });
  }

  /** 拉取后端工具列表。 */
  listTools(): Promise<{ tools: Array<{ name: string; description: string; schema?: unknown }> }> {
    return this.requestRpc("session.list_tools");
  }

  /** 手动加载 Skill 内容，供 /skill 命令展示。 */
  loadSkill(name: string, args = ""): Promise<{ name: string | null; content: string }> {
    return this.requestRpc("session.load_skill", { name, args });
  }

  /** 查询 MCP 后台连接状态。MCP 工具可能仍在连接中，返回的是当前快照。 */
  mcpStatus(): Promise<MCPStatusPayload> {
    return this.requestRpc("session.mcp_status");
  }

  /** 用户回答 AskUserQuestionTool 的提问。selected_labels 单选给一个，多选给多个。
   * cancelled=true 表示用户取消（按 Esc/中断）；后端会让工具返回 cancelled 结果。 */
  answerQuestion(params: {
    question_id: string;
    selected_labels: string[];
    other_text?: string;
    cancelled?: boolean;
  }): string {
    return this.sendRpc("session.answer_question", params as unknown as Record<string, unknown>);
  }

  quit(): string {
    return this.sendRpc("session.quit");
  }

  close(): void {
    try {
      this.proc.stdin.end();
    } catch { /* noop */ }
  }

  get stderrLogFile(): string {
    return this.stderrLogPath;
  }
}
