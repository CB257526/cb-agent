import React from "react";
import { Text } from "ink";

interface DividerProps {
  /** 默认终端宽度 */
  width?: number;
  /** 主题语义颜色（实际 hex）；不给走 dimColor */
  color?: string;
  /** 字符 */
  char?: string;
  /** 减去多少（用于带缩进） */
  padding?: number;
  /** 居中 title，例：───── 3 new ───── */
  title?: string;
}

/**
 * 横向分隔线。简化版 Claude Code Divider —— 终端宽度走 process.stdout.columns，
 * 不做 useTerminalSize hook（避免引入更多依赖）。
 */
export function Divider({ width, color, char = "─", padding = 0, title }: DividerProps) {
  const cols = width ?? process.stdout.columns ?? 80;
  const w = Math.max(0, cols - padding);

  if (title) {
    const titleLen = title.length + 2;  // 两侧空格
    const sideLen = Math.max(0, w - titleLen);
    const left = Math.floor(sideLen / 2);
    const right = sideLen - left;
    return (
      <Text color={color} dimColor={!color}>
        {char.repeat(left)} <Text dimColor>{title}</Text> {char.repeat(right)}
      </Text>
    );
  }
  return (
    <Text color={color} dimColor={!color}>
      {char.repeat(w)}
    </Text>
  );
}
