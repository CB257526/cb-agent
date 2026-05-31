import React from "react";
import { Box } from "ink";
import { Divider } from "./Divider.js";

/**
 * Pane —— 顶部一条彩色分隔 + 横向 padding 的竖排容器。
 * 用于 user 消息块、命令选择器浮层等。
 */
export function Pane({ children, color }: { children: React.ReactNode; color?: string }) {
  return (
    <Box flexDirection="column" paddingTop={1}>
      <Divider color={color} />
      <Box flexDirection="column" paddingX={2}>
        {children}
      </Box>
    </Box>
  );
}
