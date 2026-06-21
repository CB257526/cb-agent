/**
 * cb-agent / 命令注册表（OTUI 版）。
 *
 * 从旧 ui-tui/commands.ts 移植，去掉 React 的 Dispatch/SetStateAction 依赖，改用面向
 * 新 SessionProvider 的 CommandCtx。附件/桌宠类命令待 M7/M8 接入对应面板后再补。
 *
 * 每个命令是纯函数，拿到 ctx（transport + 状态操作）后做副作用。SlashCommandPicker
 * 从这里读 name/description；用户选中或回车完整输入时调 handler。
 */

import type { Transport } from "./transport.js";
import { basename } from "node:path";
import { statSync } from "node:fs";
import type { ChatItem, ContextWindow, MCPStatusPayload, PetState, QueuedAttachment, DialogSpec, SessionPayload } from "./types.js";
import { readClipboardImageAttachment } from "./clipboardImage.js";

export interface CommandCtx {
  transport: Transport;
  /** 用户输入的完整命令行，例如 "/switch session_xxx"。 */
  input: string;
  /** 去掉命令名后的参数文本。 */
  args: string;
  appendSystem: (text: string) => void;
  /** 替换整个对话流（/clear 用）。 */
  setItems: (items: ChatItem[]) => void;
  /** Reset visible Context and token usage counters after local clearing. */
  resetSessionStats: () => void;
  /** Refresh the visible Context counter from a management RPC response. */
  setContextWindow: (contextWindow: ContextWindow | null) => void;
  /** 切换后端日志面板。 */
  toggleActivity: () => void;
  /** 当前待随下一条 prompt 一起提交的附件队列。 */
  attachments: QueuedAttachment[];
  /** 维护附件队列；命令只管队列，不直接调用 OCR/ASR（后端做）。 */
  setAttachments: (updater: (prev: QueuedAttachment[]) => QueuedAttachment[]) => void;
  /** 更新桌宠状态，供 /pet 命令同步 Sidebar。 */
  setPet: (state: PetState | null) => void;
  /** 标记命令级长操作进行中（驱动 Footer 动效）；传 null 结束。runCommand 会兜底清。 */
  setPending: (label: string | null) => void;
  /** 打开浮层 Select 弹窗（方向键选 + 回车确认）。 */
  openDialog: (spec: DialogSpec) => void;
  /** 用切换/新建会话返回的 payload 重绘对话流（恢复 history + 更新 session/上下文窗口）。 */
  applySessionPayload: (payload: SessionPayload) => void;
}

export interface SlashCommand {
  name: string; // 含开头的 '/'
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

/** 由路径构造附件队列项。size 拿不到（相对路径以后端 cwd 为准）时留空即可。 */
export function makeQueuedAttachment(
  path: string,
  source: QueuedAttachment["source"] = "direct",
): QueuedAttachment {
  const cleanPath = path.trim().replace(/^["']|["']$/g, "");
  let size: number | null = null;
  try {
    const st = statSync(cleanPath);
    if (st.isFile()) size = st.size;
  } catch {
    // 相对路径以 Python 后端 BashSession.cwd 为准，TUI 这里可能 stat 不到。
  }
  return {
    id: nextAttachmentId(),
    path: cleanPath,
    source,
    fileName: basename(cleanPath) || cleanPath,
    size,
  };
}

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return `${value}B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)}KB`;
  return `${(value / 1024 / 1024).toFixed(1)}MB`;
}

function formatQueuedAttachments(attachments: QueuedAttachment[]): string {
  if (!attachments.length) return "当前没有待发送附件。使用 /attach <path> 添加图片、音频或文档。";
  const lines = attachments.map((item, index) => {
    const size = typeof item.size === "number" ? ` ${formatBytes(item.size)}` : "";
    return `  ${index + 1}. ${item.fileName}${size} (${item.source ?? "direct"})`;
  });
  return "待发送附件：\n" + lines.join("\n");
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
    handler: ({ transport, setItems, appendSystem, resetSessionStats }) => {
      transport.clearHistory();
      setItems([]);
      resetSessionStats();
      appendSystem("对话已清空。");
    },
  },
  {
    name: "/compact",
    description: "压缩并释放当前会话上下文",
    handler: async ({ transport, appendSystem, setPending, setContextWindow }) => {
      setPending("正在压缩上下文…");
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
            `下轮将使用摘要继续（${persisted}）。`,
        );
      } catch (e) {
        appendSystem(`/compact 失败：${(e as Error).message}`);
      } finally {
        setPending(null);
      }
    },
  },
  {
    name: "/new",
    description: "新建并切换到空白会话",
    handler: async ({ transport, applySessionPayload, appendSystem }) => {
      try {
        const payload = await transport.createSession();
        applySessionPayload(payload);
        appendSystem(`已新建并切换到会话 ${payload.session?.session_id ?? "unknown"}`);
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
        applySessionPayload(payload);
        appendSystem(`已切换到会话 ${payload.session?.session_id ?? sessionId}`);
      } catch (e) {
        appendSystem(`/switch 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/sessions",
    description: "打开会话列表（方向键选，回车切换）",
    handler: async ({ transport, appendSystem, openDialog, applySessionPayload }) => {
      try {
        const result = await transport.listSessions();
        if (!result.sessions.length) {
          appendSystem("（没有本地会话）");
          return;
        }
        const currentId = result.current?.session_id;
        const options = result.sessions.map((s) => ({
          name: (s.session_id === currentId ? "▶ " : "") + s.session_id,
          description: s.active_task || s.rolling_summary || "",
          value: s.session_id,
        }));
        openDialog({
          title: "切换会话",
          options,
          onSelect: async (sessionId) => {
            try {
              const payload = await transport.switchSession(sessionId);
              applySessionPayload(payload);
              appendSystem(`已切换到会话 ${payload.session?.session_id ?? sessionId}`);
            } catch (e) {
              appendSystem(`/switch 失败：${(e as Error).message}`);
            }
          },
        });
      } catch (e) {
        appendSystem(`✗ /sessions 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/tools",
    description: "打开工具列表（小窗查看）",
    handler: async ({ transport, appendSystem, openDialog }) => {
      try {
        const result = await transport.listTools();
        if (!result.tools.length) {
          appendSystem("（后端未注册任何工具）");
          return;
        }
        const options = result.tools.map((t) => ({
          name: t.name,
          description: t.description || "",
          value: t.name,
        }));
        openDialog({ title: `已注册 ${result.tools.length} 个工具`, options });
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
    description: "打开 MCP 状态（小窗查看）",
    handler: async ({ transport, appendSystem, openDialog }) => {
      try {
        const status = await transport.mcpStatus();
        const servers = Array.isArray(status.servers) ? status.servers : [];
        if (!servers.length) {
          appendSystem(formatMCPStatus(status));
          return;
        }
        const options = servers.map((s) => {
          const parts: string[] = [];
          if (s.transport) parts.push(s.transport);
          if (s.tools_count) parts.push(`${s.tools_count} tools`);
          if (s.error) parts.push(`error=${s.error}`);
          return {
            name: `${s.status === "connected" ? "● " : s.status === "error" ? "✗ " : "○ "}${s.name}`,
            description: parts.join(" · "),
            value: s.name,
          };
        });
        const total = Number(status.total ?? servers.length);
        const connected = Number(status.connected ?? 0);
        const failed = Number(status.failed ?? 0);
        openDialog({
          title: `MCP ${status.status}（${connected}/${total} connected，${failed} failed）`,
          options,
        });
      } catch (e) {
        appendSystem(`✗ /mcp 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/pet",
    description: "管理轻量桌宠 runtime 与宠物包：/pet [子命令]",
    handler: async ({ transport, args, appendSystem, setPet }) => {
      try {
        const result = await transport.runPetCommand(args);
        setPet(result.state ?? null);
        if (result.text) appendSystem(result.text);
      } catch (e) {
        appendSystem(`✗ /pet 失败：${(e as Error).message}`);
      }
    },
  },
  {
    name: "/attach",
    description: "添加附件：/attach <path>",
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

/** 找精确匹配（回车直接执行 '/help' 这种完整输入）。 */
export function findCommand(input: string): SlashCommand | undefined {
  const trimmed = input.trim();
  const name = trimmed.split(/\s+/, 1)[0];
  return COMMANDS.find((c) => c.name === name);
}
