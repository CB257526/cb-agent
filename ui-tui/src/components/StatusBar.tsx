import React from "react";
import { Box, Text } from "ink";

export interface StatusBarProps {
  model: string;
  promptTokens: number;
  completionTokens: number;
  round: number;
  maxRounds: number;
  busy: boolean;
}

/** 底部状态栏：模型名 / token 统计 / 当前 round / 提示快捷键。 */
export function StatusBar({ model, promptTokens, completionTokens, round, maxRounds, busy }: StatusBarProps) {
  const totalK = ((promptTokens + completionTokens) / 1000).toFixed(1);
  return (
    <Box flexDirection="row" justifyContent="space-between">
      <Box>
        <Text color="gray">{model}</Text>
        <Text color="gray">  │  </Text>
        <Text color="gray">tokens {totalK}k</Text>
        {round > 0 && (
          <>
            <Text color="gray">  │  </Text>
            <Text color="gray">round {round}/{maxRounds}</Text>
          </>
        )}
      </Box>
      <Box>
        {busy
          ? <Text color="yellow">working… (Ctrl-C 中断)</Text>
          : <Text color="gray">Enter 发送 · Ctrl-C 退出</Text>}
      </Box>
    </Box>
  );
}
