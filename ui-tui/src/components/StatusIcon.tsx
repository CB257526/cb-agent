import React from "react";
import { Text } from "ink";

const ICONS = {
  success: { ch: "✓", color: "success" as const },
  error:   { ch: "✗", color: "error" as const },
  warning: { ch: "⚠", color: "warning" as const },
  info:    { ch: "ℹ", color: "suggestion" as const },
  pending: { ch: "○", color: undefined },
  loading: { ch: "…", color: undefined },
} as const;

import { theme } from "../theme.js";

export type StatusKind = keyof typeof ICONS;

/** 状态指示符。颜色走主题语义，不是硬编码。 */
export function StatusIcon({ status, withSpace = false }: { status: StatusKind; withSpace?: boolean }) {
  const { ch, color } = ICONS[status];
  return (
    <Text color={color ? theme[color] : undefined} dimColor={!color}>
      {ch}{withSpace && " "}
    </Text>
  );
}
