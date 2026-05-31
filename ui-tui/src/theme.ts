/**
 * cb-agent TUI 单一暗色主题。
 *
 * 颜色全部用 ink 5 支持的命名色 + hex 字面量，避免引入 chalk RGB（ink 自身在 256 色 / truecolor
 * 之间会按终端能力降级，hex 比 rgb() 更稳）。
 *
 * 命名遵循 Claude Code 主题语义而不是字面颜色：用 `theme.success` 而不是 `"green"`，将来想统一
 * 调整或加 light 主题只改这一处。
 */

export const theme = {
  /** 主品牌色，cb-agent banner / 重点边框 */
  primary: "#88c0ff",
  /** 浅蓝，用于 user 消息 Pane 顶线 */
  accent: "#7aa2f7",
  /** 工具/选择项的高亮 */
  suggestion: "#82aaff",
  /** 暖橙，权限/中断等"需要注意但不是错"的 Pane 顶线 */
  permission: "#f7c47a",

  /** 状态语义色 */
  success: "#9ece6a",
  error: "#f7768e",
  warning: "#e0af68",
  info: "#7dcfff",

  /** 文本 */
  text: undefined as undefined | string,  // 默认前景，让终端自己定
  textMuted: "gray",                       // ink 命名色，等价于 dimColor
  textInverse: "black",

  /** 边框/分隔 */
  border: "gray",
  borderActive: "#88c0ff",

  /** 工具调用块的低调边框 */
  bashBorder: "#5c6370",

  /** assistant Byline 标识颜色 */
  claude: "#bb9af7",
} as const;

export type ThemeKey = keyof typeof theme;
