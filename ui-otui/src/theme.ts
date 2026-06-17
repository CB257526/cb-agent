/**
 * cb-agent OTUI 暗色主题。
 *
 * 在旧 ui-tui/theme.ts 的语义命名基础上，补齐 OpenTUI 需要的实心背景色
 * （background / backgroundPanel / backgroundElement）以及边框激活色。
 * OpenTUI 的 box/text 走 truecolor，统一用 hex 字面量。
 *
 * 命名遵循语义而非字面颜色：用 theme.success 而不是 "green"，将来调色或加
 * light 主题只改这一处。
 */

export const theme = {
  /** 主品牌色：banner / 重点边框 */
  primary: "#88c0ff",
  /** 浅蓝：user 消息左边框 */
  accent: "#7aa2f7",
  /** 工具/选择项高亮 */
  suggestion: "#82aaff",
  /** 暖橙：权限/中断等"需注意但非错误" */
  permission: "#f7c47a",

  /** 状态语义色 */
  success: "#9ece6a",
  error: "#f7768e",
  warning: "#e0af68",
  info: "#7dcfff",

  /** 文本 */
  text: "#c0caf5",
  textMuted: "#565f89",
  textInverse: "#1a1b26",

  /** 背景层次（OpenTUI 用实心色填充） */
  background: "#1a1b26",
  backgroundPanel: "#1f2335",
  backgroundElement: "#292e42",

  /** 边框/分隔 */
  border: "#3b4261",
  borderActive: "#88c0ff",

  /** 工具调用块的低调边框 */
  bashBorder: "#5c6370",

  /** assistant 标识颜色 */
  agent: "#bb9af7",

  /** markdown 正文（done 后解析时用） */
  markdownText: "#c0caf5",
} as const;

export type ThemeKey = keyof typeof theme;
