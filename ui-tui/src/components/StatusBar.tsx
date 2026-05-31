import React from "react";
import { Box, Text } from "ink";
import { Spinner } from "./Spinner.js";
import { Byline } from "./Byline.js";
import { KeyboardShortcutHint } from "./KeyboardShortcutHint.js";
import { theme } from "../theme.js";

export interface StatusBarProps {
  model: string;
  promptTokens: number;
  completionTokens: number;
  round: number;
  maxRounds: number;
  busy: boolean;
}

/** 底部状态栏：左边 spinner + 模型/token/round；右边快捷键 byline。 */
export function StatusBar({ model, promptTokens, completionTokens, round, maxRounds, busy }: StatusBarProps) {
  const totalK = ((promptTokens + completionTokens) / 1000).toFixed(1);
  return (
    <Box flexDirection="row" justifyContent="space-between">
      <Box>
        {busy && <Text color={theme.warning}><Spinner color={theme.warning} /> </Text>}
        <Text dimColor>
          <Byline>
            <Text>{model}</Text>
            <Text>tokens {totalK}k</Text>
            {round > 0 ? <Text>round {round}/{maxRounds}</Text> : null}
            {busy ? <Text color={theme.warning}>working…</Text> : null}
          </Byline>
        </Text>
      </Box>
      <Box>
        <Text dimColor>
          <Byline>
            <KeyboardShortcutHint shortcut="Enter" action="send" />
            <KeyboardShortcutHint shortcut="↑/↓" action="history" />
            <KeyboardShortcutHint shortcut="/" action="commands" />
            <KeyboardShortcutHint shortcut="Ctrl-O" action="log" />
            <KeyboardShortcutHint shortcut="Ctrl-C" action={busy ? "cancel" : "exit"} />
          </Byline>
        </Text>
      </Box>
    </Box>
  );
}
