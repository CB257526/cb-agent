import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import { Byline } from "./Byline.js";

/**
 * cb-agent 启动 banner。
 *
 * 这里故意使用纯 ASCII 字符画，而不是块状字符或 figlet 字体：
 * 1. 不同终端、字体和编码设置对块状字符的兼容性差，容易显示成乱码。
 * 2. 纯 ASCII 仍然能保留“开屏标志”的视觉存在感，同时不会依赖特殊字形。
 * 3. 字符画明确拼出 `cbagent`，避免上一版单行文本过于不显眼。
 */
const LOGO_LINES = [
  "        _                          _   ",
  "   ___ | |__   __ _  __ _  ___ _ __ | |_ ",
  "  / __|| '_ \\ / _` |/ _` |/ _ \\ '_ \\| __|",
  " | (__ | |_) | (_| | (_| |  __/ | | | |_ ",
  "  \\___||_.__/ \\__,_|\\__, |\\___|_| |_|\\__|",
  "                     |___/                ",
];

export function Banner({ model, cwd }: { model: string; cwd: string }) {
  const shortCwd = shortenCwd(cwd);
  return (
    <Box flexDirection="column" marginBottom={1}>
      {LOGO_LINES.map((line, i) => (
        <Text key={i} color={theme.primary} bold>
          {line}
        </Text>
      ))}
      <Box marginTop={0}>
        <Text dimColor>
          <Byline>
            <Text color={theme.agent} bold>{model}</Text>
            <Text>{shortCwd}</Text>
            <Text>Ctrl-O for log</Text>
            <Text>/ for commands</Text>
          </Byline>
        </Text>
      </Box>
    </Box>
  );
}

function shortenCwd(cwd: string): string {
  // Windows 路径过长时只保留最后两级，让 banner byline 在窄终端里也能放得下。
  const parts = cwd.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 2) return cwd;
  return ".../" + parts.slice(-2).join("/");
}
