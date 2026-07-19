/**
 * cb-agent OTUI 的终端原生主题。
 *
 * 与网页界面不同，终端用户通常已经选择了自己可读的前景色和背景色。这里使用
 * OpenTUI 的 default/indexed 颜色意图，让颜色最终交给终端调色板解释，避免固定
 * truecolor 在浅色主题或高对比主题中失去可读性。
 */

import { RGBA, SyntaxStyle, TextAttributes } from "@opentui/core";

const ansi = {
  red: RGBA.fromIndex(1),
  green: RGBA.fromIndex(2),
  magenta: RGBA.fromIndex(5),
  cyan: RGBA.fromIndex(6),
  subtle: RGBA.fromIndex(8),
} as const;

export const theme = {
  /** 输入、选择和主要交互统一使用青色。 */
  primary: ansi.cyan,
  accent: ansi.cyan,
  suggestion: ansi.cyan,
  info: ansi.cyan,

  /** Agent、计划和需注意状态使用洋红色，避免引入额外蓝黄体系。 */
  agent: ansi.magenta,
  permission: ansi.magenta,
  warning: ansi.magenta,

  /** 成功与失败沿用终端最稳定的绿红语义。 */
  success: ansi.green,
  error: ansi.red,

  /** 正文和背景跟随用户终端；次要色仅用于无法设置 DIM 属性的控件。 */
  text: RGBA.defaultForeground(),
  textMuted: ansi.subtle,
  textInverse: RGBA.defaultBackground(),
  markdownText: RGBA.defaultForeground(),

  /** 主画布和弹窗都使用终端默认背景；保持自适应的同时确保重绘能清除旧字符。 */
  background: RGBA.defaultBackground(),
  backgroundPanel: RGBA.defaultBackground(),
  backgroundElement: RGBA.defaultBackground(),

  /** 边框只承担结构提示，不参与品牌表达。 */
  border: ansi.subtle,
  borderActive: ansi.cyan,
  bashBorder: ansi.subtle,

  /** 选区必须使用显式不透明颜色；否则默认前景色无法作为可见的选区背景。 */
  selectionBackground: RGBA.fromIndex(14),
  selectionForeground: RGBA.fromIndex(0),
} as const;

/** 文字层级优先通过属性表达，避免把“弱化”绑定到某个固定灰色。 */
export const textAttributes = {
  muted: TextAttributes.DIM,
  mutedItalic: TextAttributes.DIM | TextAttributes.ITALIC,
  selected: TextAttributes.BOLD,
} as const;

/**
 * Markdown 内部使用独立的文本缓冲，不会稳定继承外层 `<markdown fg>`。为常用 markup
 * scope 显式注册终端色，既保证正文可见，也避免退回固定 truecolor 主题。
 */
export function createMarkdownSyntaxStyle(): SyntaxStyle {
  return SyntaxStyle.fromStyles({
    default: { fg: theme.markdownText },
    "markup.heading": { fg: theme.agent, bold: true },
    "markup.strong": { fg: theme.text, bold: true },
    "markup.italic": { fg: theme.text, italic: true },
    "markup.raw": { fg: theme.success },
    "markup.link": { fg: theme.primary },
    "markup.link.url": { fg: theme.primary, underline: true },
    "markup.link.label": { fg: theme.primary },
    "markup.quote": { fg: theme.success, dim: true },
    "markup.list": { fg: theme.primary },
    conceal: { fg: theme.textMuted, dim: true },
  });
}

export type ThemeKey = keyof typeof theme;
