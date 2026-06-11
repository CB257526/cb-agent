/**
 * cb-agent / 命令注册表。
 *
 * 设计：每个命令是一个纯函数，拿到 ctx（transport / 状态 setter）后做副作用。
 * 命令面板（SlashCommandPicker）从这里读 name/description；用户选中时调 handler。
 *
 * 添加新命令只需扩 COMMANDS 数组——不要把命令逻辑分散到 App.tsx 里。
 */

import type { Transport } from "./transport.js";
import { basename } from "node:path";
import { statSync } from "node:fs";
import type { Dispatch, SetStateAction } from "react";
import type { ChatItem, ContextWindow, MCPStatusPayload, PetState, QueuedAttachment, SessionPayload } from "./types.js";
import { readClipboardImageAttachment } from "./clipboardImage.js";

export interface CommandCtx {
  transport: Transport;
  /** 用户输入的完整命令行，例如 "/switch session_xxx"。 */
  input: string;
  /** 去掉命令名后的参数文本。 */
  args: string;
  appendSystem: (text: string) => void;
  /** 替换整个对话流（/clear 用） */
  setItems: (updater: (prev: ChatItem[]) => ChatItem[]) => void;
  /** 用后端恢复的 history 重绘当前会话。 */
  applySessionPayload: (payload: SessionPayload, notice?: string) => void;
  /** 更新底部 Context 上下文窗口指标。 */
  setContextWindow: (contextWindow: ContextWindow | null) => void;
  /** 将底部 Context 指标重置为 0，同时保留当前窗口上限。 */
  resetContextWindow: () => void;
  /** 打开可见的会话切换面板。 */
  openSessionSwitcher: () => void;
  /** 切换后端日志面板 */
  toggleActivity: () => void;
  /** 更新桌宠附属状态，供 /pet 命令和事件同步使用。 */
  setPetState: (state: PetState | null) => void;
  /** 当前待随下一条 prompt 一起提交的附件队列。 */
  attachments: QueuedAttachment[];
  /** 更新附件队列；命令只维护队列，不直接调用 OCR/ASR。 */
  setAttachments: Dispatch<SetStateAction<QueuedAttachment[]>>;
}

export interface SlashCommand {
  name: string;          // 含开头的 '/'
  description: string;
  handler: (ctx: CommandCtx) => void | Promise<void>;
}

export function formatMCPStatus(status: MCPStatusPayload): string {
  const total = Number(status.total ?? status.servers?.length ?? 0);
  const connected = Number(status.connected ?? 0);
  const failed = Number(status.failed ?? 0);
  const state = status.status || "unknown";
  const lines = [`MCP 状态：${state}（${connected}/${total} connected，${failed} failed）`];

  const servers = Array.isArray(status.servers) ? status.servers : [];
  if (!servers.length) {
    if (status.error) lines.push(`  • ${status.error}`);
    return lines.join("\n");
  }

  for (const server of servers) {
    const name = server.name || "unknown";
    const serverState = server.status || "unknown";
    const parts: string[] = [];
    if (server.transport) parts.push(`transport=${server.transport}`);
    if (server.tools_count) parts.push(`tools=${server.tools_count}`);
    if (server.elapsed_seconds) parts.push(`${server.elapsed_seconds}s`);
    if (server.error) parts.push(`error=${server.error}`);
    const suffix = parts.length ? ` (${parts.join(", ")})` : "";
    lines.push(`  • ${name}: ${serverState}${suffix}`);
  }
  return lines.join("\n");
}

let attachmentSeq = 0;

function nextAttachmentId(): string {
  attachmentSeq += 1;
  return `att_${Date.now()}_${attachmentSeq}`;
}

export function makeQueuedAttachment(path: string, source: QueuedAttachment["source"] = "direct"): QueuedAttachment {
  const cleanPath = path.trim().replace(/^["']|["']$/g, "");
  let size: number | null = null;
  try {
    const st = statSync(cleanPath);
    if (st.isFile()) size = st.size;
  } catch {
    // 相对路径以 Python 后端 BashSession.cwd 为准，TUI 这里可能 stat 不到；保留未知大小即可。
  }
  return {
    id: nextAttachmentId(),
    path: cleanPath,
    source,
    fileName: basename(cleanPath) || cleanPath,
    size,
  };
}

function formatQueuedAttachments(attachments: QueuedAttachment[]): string {
  if (!attachments.length) return "当前没有待发送附件。使用 /attach <path> 添加图片或音频。";
  const lines = attachments.map((item, index) => {
    const size = typeof item.size === "number" ? ` ${formatBytes(item.size)}` : "";
    return `  ${index + 1}. ${item.fileName}${size} (${item.source ?? "direct"})`;
  });
  return "待发送附件：\n" + lines.join("\n");
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return `${value}B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)}KB`;
  return `${(value / 1024 / 1024).toFixed(1)}MB`;
}

export const COMMANDS: readonly SlashCommand[] = [
  {
    name: "/help",
    description: "列出所有可用命令",
    handler: ({ appendSystem }) => {
      const lines = COMMANDS.map((c) => `  ${c.name.padEnd(10)} ${c.description}`);
      appendSystem("可用命令：\n" + lines.join("\n"));
    },
  },
  {
    name: "/clear",
    description: "清空对话历史（前后端都清）",
    handler: ({ transport, setItems, appendSystem, resetContextWindow }) => {
      transport.clearHistory();
      setItems(() => []);
      resetContextWindow();
      appendSystem("对话已清空。");
    },
  },
  {
    name: "/compact",
    description: "压缩并释放当前会话上下文",
    handler: async ({ transport, appendSystem, setContextWindow }) => {
      try {
        const payload = await transport.compactSession();
        if (payload.context_window !== undefined) {
          setContextWindow(payload.context_window ?? null);
        }
        if (payload.no_op) {
          appendSystem("当前没有可压缩的上下文。");
          return;
        }
        const persisted = payload.persisted ? "已落盘" : "未落盘";
        appendSystem(
          `已压缩上下文：history ${payload.before_messages} -> ${payload.after_messages}，` +
          `下轮将使用摘要继续（${persisted}）。`
        );
      } catch (e) {
        appendSystem(`/compact 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/sessions",
    description: "打开本地会话切换面板",
    handler: ({ openSessionSwitcher }) => {
      openSessionSwitcher();
    },
  },
  {
    name: "/new",
    description: "新建并切换到空白会话",
    handler: async ({ transport, applySessionPayload, appendSystem }) => {
      try {
        const payload = await transport.createSession();
        applySessionPayload(payload, `已新建并切换到会话 ${payload.session?.session_id ?? "unknown"}`);
      } catch (e) {
        appendSystem(`/new 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/switch",
    description: "切换到指定会话：/switch <id>",
    handler: async ({ transport, args, applySessionPayload, appendSystem }) => {
      const sessionId = args.trim();
      if (!sessionId) {
        appendSystem("用法：/switch <session_id>");
        return;
      }
      try {
        const payload = await transport.switchSession(sessionId);
        applySessionPayload(payload, `已切换到会话 ${payload.session?.session_id ?? sessionId}`);
      } catch (e) {
        appendSystem(`/switch 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/tools",
    description: "列出后端注册的工具",
    handler: async ({ transport, appendSystem }) => {
      try {
        const result = await transport.listTools();
        if (!result.tools.length) {
          appendSystem("（后端未注册任何工具）");
          return;
        }
        const lines = result.tools.map((t) => `  • ${t.name}  ${t.description || ""}`.trimEnd());
        appendSystem(`已注册 ${result.tools.length} 个工具：\n` + lines.join("\n"));
      } catch (e) {
        appendSystem(`✗ /tools 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/skill",
    description: "列出或手动加载 Skill：/skill [name] [args]",
    handler: async ({ transport, args, appendSystem }) => {
      const [name = "", ...rest] = args.trim().split(/\s+/);
      try {
        const result = await transport.loadSkill(name, rest.join(" "));
        appendSystem(result.content);
      } catch (e) {
        appendSystem(`✗ /skill 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/mcp",
    description: "查看 MCP 后台连接状态",
    handler: async ({ transport, appendSystem }) => {
      try {
        const status = await transport.mcpStatus();
        appendSystem(formatMCPStatus(status));
      } catch (e) {
        appendSystem(`✗ /mcp 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/pet",
    description: "管理轻量桌宠 runtime 与宠物包",
    handler: async ({ transport, args, appendSystem, setPetState }) => {
      try {
        const result = await transport.runPetCommand(args);
        setPetState(result.state ?? null);
        if (result.text) appendSystem(result.text);
      } catch (e) {
        appendSystem(`✗ /pet 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/attach",
    description: "添加图片或音频附件：/attach <path>",
    handler: ({ args, appendSystem, setAttachments }) => {
      const path = args.trim();
      if (!path) {
        appendSystem("用法：/attach <path>");
        return;
      }
      const item = makeQueuedAttachment(path, "direct");
      setAttachments((prev) => [...prev, item]);
      const size = typeof item.size === "number" ? `，${formatBytes(item.size)}` : "";
      appendSystem(`已添加附件：${item.fileName}${size}。发送下一条消息时会一起提交。`);
    },
  },
  {
    name: "/paste-image",
    description: "从系统剪贴板读取图片并加入附件队列",
    handler: async ({ appendSystem, setAttachments }) => {
      try {
        const item = await readClipboardImageAttachment();
        setAttachments((prev) => [...prev, item]);
        appendSystem(`已从剪贴板添加图片：${item.fileName}。发送下一条消息时会一起提交。`);
      } catch (e) {
        appendSystem(`剪贴板图片读取失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/attachments",
    description: "查看待发送附件队列",
    handler: ({ attachments, appendSystem }) => {
      appendSystem(formatQueuedAttachments(attachments));
    },
  },
  {
    name: "/detach",
    description: "移除附件：/detach <index|all>",
    handler: ({ args, attachments, appendSystem, setAttachments }) => {
      const arg = args.trim().toLowerCase();
      if (!arg) {
        appendSystem("用法：/detach <index|all>");
        return;
      }
      if (arg === "all") {
        const count = attachments.length;
        setAttachments(() => []);
        appendSystem(`已清空 ${count} 个待发送附件。`);
        return;
      }
      const index = Number.parseInt(arg, 10);
      if (!Number.isInteger(index) || index < 1 || index > attachments.length) {
        appendSystem(`附件序号超出范围：${arg}`);
        return;
      }
      const removed = attachments[index - 1];
      setAttachments((prev) => prev.filter((_, i) => i !== index - 1));
      appendSystem(`已移除附件：${removed.fileName}`);
    },
  },
  {
    name: "/log",
    description: "切换后端日志面板（等同 Ctrl-O）",
    handler: ({ toggleActivity }) => {
      toggleActivity();
    },
  },
];

/** 简单 prefix 过滤；query 不带 '/'。 */
export function filterCommands(query: string): SlashCommand[] {
  const q = query.toLowerCase();
  return COMMANDS.filter((c) => c.name.slice(1).toLowerCase().startsWith(q));
}

/** 找精确匹配（用于回车直接执行 '/help' 这种完整输入）。 */
export function findCommand(input: string): SlashCommand | undefined {
  const trimmed = input.trim();
  const name = trimmed.split(/\s+/, 1)[0];
  return COMMANDS.find((c) => c.name === name);
}
