/**
 * 统一配置 OpenTUI 文本选区颜色。
 *
 * OpenTUI 未指定 selectionBg/selectionFg 时，会把每个文本节点自己的前景色当作
 * 选区背景。终端默认前景色不能可靠地作为背景绘制，因此默认文本会出现“选中了但
 * 看不见高亮”的现象。这里在每次渲染前遍历文本缓冲，覆盖普通文本、Markdown 内部
 * 代码块和输入控件的选区颜色。
 */

import { TextBufferRenderable, type BaseRenderable } from "@opentui/core";
import { theme } from "./theme.js";

export function applySelectionColors(root: BaseRenderable): void {
  if (root instanceof TextBufferRenderable) {
    root.selectionBg = theme.selectionBackground;
    root.selectionFg = theme.selectionForeground;
  }

  for (const child of root.getChildren()) {
    applySelectionColors(child);
  }
}
