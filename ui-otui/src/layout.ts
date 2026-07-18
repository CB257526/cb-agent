/**
 * OTUI 的响应式布局规则。
 *
 * 所有宽度断点集中在这里，避免 Header、Footer、弹窗和输入框各自推导出略有差异
 * 的“窄屏”定义。函数保持纯函数，便于使用多个终端尺寸做确定性测试。
 */

export type LayoutMode = "wide" | "medium" | "narrow";

export function getLayoutMode(width: number): LayoutMode {
  if (width >= 100) return "wide";
  if (width >= 68) return "medium";
  return "narrow";
}

export function getHorizontalPadding(width: number): number {
  return width >= 80 ? 2 : 1;
}

export function getPromptMaxHeight(height: number): number {
  return Math.max(3, Math.min(8, Math.floor(height / 4)));
}

export interface DialogMetrics {
  width: number;
  maxHeight: number;
}

export function getDialogMetrics(width: number, height: number): DialogMetrics {
  return {
    width: Math.max(1, Math.min(72, width - 4)),
    maxHeight: Math.max(1, height - 4),
  };
}

export interface VisibleWindow<T> {
  items: T[];
  start: number;
}

/**
 * 截取围绕当前选中项的可见窗口。
 *
 * 选中项靠近列表尾部时窗口会向前补齐，保证弹窗和命令面板不会因为末尾空行而抖动。
 */
export function sliceAroundSelection<T>(
  items: readonly T[],
  selectedIndex: number,
  maxVisible: number,
): VisibleWindow<T> {
  const count = Math.max(1, Math.floor(maxVisible));
  if (items.length <= count) return { items: [...items], start: 0 };

  const selected = Math.max(0, Math.min(items.length - 1, selectedIndex));
  const preferredStart = selected - Math.floor(count / 2);
  const start = Math.max(0, Math.min(items.length - count, preferredStart));
  return { items: items.slice(start, start + count), start };
}

export function shortSessionId(sessionId: string): string {
  const parts = String(sessionId ?? "").split("_");
  if (parts.length >= 4) return `${parts[1]}_${parts[2]}_${parts[3]}`;
  return String(sessionId ?? "");
}
