/**
 * cb-agent / 命令注册表。
 *
 * 设计：每个命令是一个纯函数，拿到 ctx（transport / 状态 setter）后做副作用。
 * 命令面板（SlashCommandPicker）从这里读 name/description；用户选中时调 handler。
 *
 * 添加新命令只需扩 COMMANDS 数组——不要把命令逻辑分散到 App.tsx 里。
 */

import type { Transport } from "./transport.js";
import type { ChatItem } from "./types.js";

export interface CommandCtx {
  transport: Transport;
  appendSystem: (text: string) => void;
  /** 替换整个对话流（/clear 用） */
  setItems: (updater: (prev: ChatItem[]) => ChatItem[]) => void;
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
  return COMMANDS.find((c) => c.name === trimmed);
}
