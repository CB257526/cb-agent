import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import { Byline } from "./Byline.js";

/**
 * cb-agent 开屏 banner。
 *
 * ASCII art logo（手写小字体，避免引入 figlet 增加打包体积）+ 副标题 byline。
 * 行高紧凑：5 行 logo + 1 行 byline + 1 行间距。终端高度小时仍可用。
 */
const LOGO_LINES = [
  "  ▄▄▄▄▄  ▄▄▄▄▄    ▄▄▄  ▄▄▄▄▄ ▄▄▄▄▄ ▄▄▄  ▄▄▄ ▄▄▄▄▄",
  " ██      ██   ██ ██   ██  ██  ██▄▄▄ ██▀▀▄ ██   ▀█▀ ",
  " ██      ██▄▄▄██ ██▄▄▄██  ██  ██    ██  ██  ██  █  ",
  " ██   █  ██   ██ ██   ██  ██  ██    ██  ██   ██▀   ",
  "  ▀▀▀▀▀  ▀▀▀▀▀▀  ▀▀  ▀▀  ▀▀▀  ▀▀▀▀▀ ▀▀  ▀▀    ▀    ",
];

export function Banner({ model, cwd }: { model: string; cwd: string }) {
  const shortCwd = shortenCwd(cwd);
  return (
    <Box flexDirection="column" marginBottom={1}>
      {LOGO_LINES.map((line, i) => (
        <Text key={i} color={theme.primary}>{line}</Text>
      ))}
      <Box marginTop={0}>
        <Text dimColor>
          <Byline>
            <Text color={theme.claude} bold>{model}</Text>
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
  // Windows 路径太长时只留最后两级
  const parts = cwd.replace(/\\/g, "/").split("/").filter(Boolean);
  if (parts.length <= 2) return cwd;
  return ".../" + parts.slice(-2).join("/");
}
