import React, { Children, isValidElement } from "react";
import { Text } from "ink";

/**
 * Byline —— 用 ` · ` 把多个 children 串起来。null/undefined/false 会被自动过滤，
 * 适合在 prompt hint / 状态行做"快捷键 1 · 快捷键 2"这种紧凑展示。
 */
export function Byline({ children, separator = " · " }: { children: React.ReactNode; separator?: string }) {
  const list = Children.toArray(children);
  if (list.length === 0) return null;
  return (
    <>
      {list.map((c, i) => (
        <React.Fragment key={isValidElement(c) ? (c.key ?? i) : i}>
          {i > 0 && <Text dimColor>{separator}</Text>}
          {c}
        </React.Fragment>
      ))}
    </>
  );
}
