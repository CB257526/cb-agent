/**
 * cb-agent / 命令注册表。
 *
 * 设计：每个命令是一个纯函数，拿到 ctx（transport / 状态 setter）后做副作用。
 * 命令面板（SlashCommandPicker）从这里读 name/description；用户选中时调 handler。
 *
 * 添加新命令只需扩 COMMANDS 数组——不要把命令逻辑分散到 App.tsx 里。
 */

import type { Transport } from "./transport.js";
import type { ChatItem, SessionPayload } from "./types.js";

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
  /** 打开可见的会话切换面板。 */
  openSessionSwitcher: () => void;
  /** 切换后端日志面板 */
  toggleActivity: () => void;
}

export interface SlashCommand {
  name: string;          // 含开头的 '/'
  description: string;
  handler: (ctx: CommandCtx) => void | Promise<void>;
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
    handler: ({ transport, setItems, appendSystem }) => {
      transport.clearHistory();
      setItems(() => []);
      appendSystem("对话已清空。");
    },
  },
  {
    name: "/compact",
    description: "压缩并释放当前会话上下文",
    handler: async ({ transport, appendSystem }) => {
      try {
        const payload = await transport.compactSession();
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
