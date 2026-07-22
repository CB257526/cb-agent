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
import { delimiter, dirname, isAbsolute, join, resolve } from "node:path";
import { AgentEvent, CacheStatsPayload, CompactPayload, MCPStatusPayload, ModelListPayload, ModelSwitchPayload, PermissionMode, PlanMode, PlanState, PromptAttachmentInput, SessionPayload, SessionSummary } from "./types.js";
import { appendOtuiDiagnostic } from "./diagnostics.js";

export function parseBackendArgs(rawArgs: string[]): string[] {
  const accepted: string[] = [];
  for (let index = 0; index < rawArgs.length; index += 1) {
    const arg = rawArgs[index];
    if (["--no-mcp", "--no-ctx", "--dangerously-skip-permissions"].includes(arg)) {
      accepted.push(arg);
      continue;
    }
    if (arg.startsWith("--memory-system=")) {
      const value = arg.slice("--memory-system=".length);
      if (!["light", "full", "off"].includes(value)) {
        throw new Error(`不支持的记忆系统: ${value}`);
      }
      accepted.push(arg);
      continue;
    }
    if (arg === "--memory-system") {
      const value = rawArgs[index + 1];
      if (!["light", "full", "off"].includes(value)) {
        throw new Error("--memory-system 需要 light、full 或 off");
      }
      accepted.push(arg, value);
      index += 1;
      continue;
    }
    throw new Error(`OTUI 不支持启动参数: ${arg}`);
  }
  return accepted;
}

export function buildRunAgentArgs(
  agentRoot: string,
  env: NodeJS.ProcessEnv = process.env,
  backendArgs: string[] = [],
): string[] {
  const args = [
    join(agentRoot, "run_agent.py"),
    "--transport",
    "jsonrpc",
  ];
  if (!backendArgs.some((arg) => arg === "--memory-system" || arg.startsWith("--memory-system="))) {
    args.push("--memory-system", "light");
  }
  args.push(...backendArgs);
  if (
    isTruthyEnv(env.CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS)
    && !backendArgs.includes("--dangerously-skip-permissions")
  ) {
    args.push("--dangerously-skip-permissions");
  }
  return args;
}

export function buildBackendEnv(
  env: NodeJS.ProcessEnv,
  backendArgs: string[] = [],
): NodeJS.ProcessEnv {
  const memorySystemIndex = backendArgs.indexOf("--memory-system");
  const enablesFullMemory = backendArgs.includes("--memory-system=full")
    || (memorySystemIndex >= 0 && backendArgs[memorySystemIndex + 1] === "full");
  if (!enablesFullMemory) return { ...env };
  return {
    ...env,
    CBAGENT_ENABLE_FULL_MEMORY: "1",
  };
}

export const STDERR_UI_LINE_MAX = 4000;

export function defaultGatewayLogPath(cwd: string, now = Date.now()): string {
  return join(cwd, ".cbagent", "logs", "system", `gateway-${now}.log`);
}

function isTruthyEnv(value: string | undefined): boolean {
  return ["1", "true", "yes", "on"].includes((value ?? "").trim().toLowerCase());
}

function clipStderrForUi(line: string): string {
  if (line.length <= STDERR_UI_LINE_MAX) return line;
  const omitted = line.length - STDERR_UI_LINE_MAX;
  return `${line.slice(0, STDERR_UI_LINE_MAX)}…（实时日志已截断 ${omitted} 字符，完整内容见日志文件）`;
}

function pathWithPythonDir(python: string, env: NodeJS.ProcessEnv): string | undefined {
  if (!isAbsolute(python)) return env.PATH;
  const pythonDir = dirname(python);
  const current = env.PATH ?? "";
  return current ? `${pythonDir}${delimiter}${current}` : pythonDir;
}

export interface TransportOptions {
  /** Python 解释器路径。默认环境变量 CB_AGENT_PYTHON 或 "python"。 */
  python?: string;
  /** cb-agent 安装目录，用于定位 run_agent.py。默认使用当前目录的父目录。 */
  agentRoot?: string;
  /** 用户工作目录。Python 子进程从这里启动，项目级 .cbagent 状态也写到这里。 */
  workspaceCwd?: string;
  /** @deprecated 兼容旧调用；未提供 workspaceCwd/agentRoot 时同时作为两者默认值。 */
  cwd?: string;
  /** 额外环境变量 */
  env?: NodeJS.ProcessEnv;
  /** 传给 Python JSON-RPC 后端的受支持启动参数。 */
  backendArgs?: string[];
  /** stderr 日志路径。默认 .cbagent/logs/system/gateway-<ts>.log */
  stderrLog?: string;
}

export interface TransportEvents {
  event: (ev: AgentEvent) => void;
  response: (id: string | number, body: { result?: unknown; error?: { code: number; message: string } }) => void;
  exit: (code: number | null, signal?: NodeJS.Signals | null) => void;
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
  private closed = false;

  constructor(opts: TransportOptions = {}) {
    super();
    const python = opts.python ?? process.env.CB_AGENT_PYTHON ?? "python";
    const legacyRoot = opts.cwd ?? join(process.cwd(), "..");
    const agentRoot = resolve(opts.agentRoot ?? legacyRoot);
    const workspaceCwd = resolve(opts.workspaceCwd ?? process.env.CBAGENT_WORKSPACE ?? legacyRoot);

    // 解析 stderr 日志路径
    const logsDir = join(workspaceCwd, ".cbagent", "logs", "system");
    mkdirSync(logsDir, { recursive: true });
    this.stderrLogPath = opts.stderrLog ?? defaultGatewayLogPath(workspaceCwd);
    const childEnv: NodeJS.ProcessEnv = buildBackendEnv({
      ...process.env,
      ...opts.env,
    }, opts.backendArgs);
    Object.assign(childEnv, {
      CBAGENT_APP_ROOT: agentRoot,
      CBAGENT_WORKSPACE: workspaceCwd,
      PYTHONIOENCODING: "utf-8",
    });
    childEnv.PATH = pathWithPythonDir(python, childEnv);

    this.proc = spawn(python, buildRunAgentArgs(agentRoot, childEnv, opts.backendArgs), {
      cwd: workspaceCwd,
      env: childEnv,
      stdio: ["pipe", "pipe", "pipe"],
    });
    appendOtuiDiagnostic(`spawn python=${python} agentRoot=${agentRoot} workspace=${workspaceCwd}`);

    // stdout: NDJSON 协议
    this.proc.stdout.setEncoding("utf-8");
    this.proc.stdout.on("data", (chunk: string) => this.handleStdout(chunk));
    this.proc.stdout.on("error", (err) => {
      appendOtuiDiagnostic("python stdout stream error", err);
      this.emit("protocolError", "stdout-error", err as Error);
    });

    // stderr: 同时做两件事
    //   1. 全量写日志文件（保留最完整记录，事故归档）
    //   2. 行缓冲解析后 emit stderr 事件（UI 实时面板用，行内不带换行）
    const logStream = createWriteStream(this.stderrLogPath, { flags: "a" });
    logStream.on("error", (err) => {
      appendOtuiDiagnostic(`gateway stderr log write failed: ${this.stderrLogPath}`, err);
      this.emit("stderr", `OTUI 日志写入失败：${(err as Error).message}`);
    });
    this.proc.stderr.setEncoding("utf-8");
    this.proc.stderr.on("data", (chunk: string) => {
      logStream.write(chunk);
      this.handleStderr(chunk);
    });
    this.proc.stderr.on("error", (err) => {
      appendOtuiDiagnostic("python stderr stream error", err);
      this.emit("stderr", `Python stderr stream error: ${(err as Error).message}`);
    });
    this.proc.stdin.on("error", (err) => {
      appendOtuiDiagnostic("python stdin stream error", err);
      this.emit("protocolError", "stdin-error", err as Error);
    });

    this.proc.on("exit", (code, signal) => {
      this.closed = true;
      appendOtuiDiagnostic(`python process exit code=${code ?? "?"} signal=${signal ?? "none"}`);
      logStream.end();
      this.emit("exit", code, signal);
    });
    this.proc.on("error", (err) => {
      this.closed = true;
      appendOtuiDiagnostic("python process spawn/runtime error", err);
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
      // stderr 原文已经在调用 handleStderr 前写入日志文件；这里发给 OTUI 的只是
      // 实时面板预览。超长日志如果完整进入前端状态会造成渲染压力，所以只截断预览副本。
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
    const fail = (error: unknown) => {
      const message = error instanceof Error ? error.message : String(error);
      appendOtuiDiagnostic(`RPC ${method} write failed id=${id}`, error);
      queueMicrotask(() => {
        this.emit("response", id, {
          error: { code: -32000, message: `Python agent 不可写入：${message}` },
        });
      });
    };

    if (this.closed || !this.proc.stdin.writable) {
      fail(new Error("python process is not running"));
      return id;
    }

    try {
      this.proc.stdin.write(msg, (err) => {
        if (err) fail(err);
      });
    } catch (error) {
      fail(error);
    }
    return id;
  }

  /** 发送 RPC 并等响应。timeoutMs 默认 5s；compact 等需要调 LLM 的 RPC 要传更长超时。 */
  private requestRpc<T = unknown>(
    method: string,
    params: Record<string, unknown> = {},
    timeoutMs = 5000,
  ): Promise<T> {
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
      }, timeoutMs);
      this.on("response", onResp);
    });
  }

  sendPrompt(text: string, attachments: PromptAttachmentInput[] = []): string {
    return this.sendRpc("prompt.submit", { text, attachments });
  }

  cancel(): string {
    return this.sendRpc("session.cancel");
  }

  clearHistory(): string {
    return this.sendRpc("session.clear_history");
  }

  /** 压缩当前 active 会话上下文；不重绘 UI，只返回压缩结果供命令提示。
   * 压缩要调 LLM 做摘要，耗时远超普通 RPC，给 120s 长超时。 */
  compactSession(): Promise<CompactPayload> {
    return this.requestRpc("session.compact", {}, 120000);
  }

  setMode(mode: PlanMode): Promise<{ mode: PlanMode; plan_state: PlanState; session?: SessionSummary | null }> {
    return this.requestRpc("session.set_mode", { mode });
  }

  setPermissionMode(permission_mode: PermissionMode): Promise<{ permission_mode: PermissionMode }> {
    return this.requestRpc("session.set_permission_mode", { permission_mode });
  }

  getPermissionMode(): Promise<{ permission_mode: PermissionMode }> {
    return this.requestRpc("session.get_permission_mode");
  }

  getPlanState(): Promise<{ plan_state: PlanState }> {
    return this.requestRpc("session.get_plan_state");
  }

  approvePlan(): Promise<{ approved: true; mode: "execute"; plan: string; plan_state: PlanState }> {
    return this.requestRpc("session.approve_plan");
  }

  rejectPlan(feedback: string): Promise<{ rejected: true; mode: "plan"; plan_state: PlanState }> {
    return this.requestRpc("session.reject_plan", { feedback });
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

  /** 拉取统一模型配置列表。 */
  listModels(): Promise<ModelListPayload> {
    return this.requestRpc("session.list_models");
  }

  /** 切换当前 LLM 请求目标；会话 history 不变。 */
  setModel(model_key: string): Promise<ModelSwitchPayload> {
    // 降档切换可能经历旧模型 compact 和目标模型重试，不能沿用普通 RPC 的 5 秒超时。
    return this.requestRpc("session.set_model", { model_key }, 180000);
  }

  /** 获取今天的 prompt cache 命中统计。 */
  cacheStats(): Promise<CacheStatsPayload> {
    return this.requestRpc("session.cache_stats");
  }

  /** 拉取 Skill 索引列表，供 /skill 弹窗选择。 */
  listSkills(): Promise<{ skills: Array<{
    name: string;
    description?: string;
    short_description?: string | null;
    path?: string;
  }> }> {
    return this.requestRpc("session.list_skills");
  }

  /** 手动加载 Skill 内容，供 /skill <name> 预览正文。 */
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
    this.closed = true;
    appendOtuiDiagnostic("transport close requested");
    try {
      this.proc.stdin.end();
    } catch { /* noop */ }
  }

  get stderrLogFile(): string {
    return this.stderrLogPath;
  }
}
