import React from "react";
import { Box, Text } from "ink";
import { Pane } from "../components/Pane.js";
import { theme } from "../theme.js";

const rarityColors: Record<string, string> = {
  common: theme.textMuted,
  uncommon: theme.success,
  rare: theme.info,
  epic: theme.agent,
  legendary: theme.warning,
};

export interface ParsedBuddyStat {
  name: string;
  bar: string;
  value: number;
}

export interface ParsedBuddyCard {
  stars: string;
  rarity: string;
  species: string;
  shiny: boolean;
  sprite: string[];
  name: string;
  personality: string;
  stats: ParsedBuddyStat[];
}

/** 将后端 /buddy status/hatch 返回的纯文本卡片解析成 TUI 可渲染结构。
 *
 * 后端已经把 Buddy 状态通过 JSON-RPC 返回给输入框旁的 sprite；这里解析文本
 * 只是为了让对话流里的 /buddy 结果也能保持一致的卡片样式。解析失败时返回
 * null，由系统消息继续走普通 Markdown 渲染，避免影响其他命令输出。
 */
export function parseBuddyCardText(text: string): ParsedBuddyCard | null {
  const lines = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n").trimEnd().split("\n");
  let i = 0;

  while (i < lines.length && !lines[i].trim()) i += 1;
  if (/^Buddy 已(?:重新)?孵化：$/.test(lines[i]?.trim() ?? "")) {
    i += 1;
  }
  while (i < lines.length && !lines[i].trim()) i += 1;

  const header = /^(\*+)\s+([A-Z]+)\s+([A-Z]+)(?:\s+(shiny))?$/.exec(lines[i]?.trim() ?? "");
  if (!header) return null;
  i += 1;

  while (i < lines.length && !lines[i].trim()) i += 1;
  const sprite: string[] = [];
  while (i < lines.length && lines[i].trim()) {
    sprite.push(lines[i]);
    i += 1;
  }
  if (!sprite.length) return null;

  while (i < lines.length && !lines[i].trim()) i += 1;
  const nameLine = /^(.+?)\s+-\s+(.+)$/.exec(lines[i]?.trim() ?? "");
  if (!nameLine) return null;
  i += 1;

  while (i < lines.length && !lines[i].trim()) i += 1;
  const stats: ParsedBuddyStat[] = [];
  for (; i < lines.length; i += 1) {
    const stat = /^([A-Z_]+)\s+([#.]{10})\s+(\d{1,3})$/.exec(lines[i].trim());
    if (!stat) continue;
    stats.push({ name: stat[1], bar: stat[2], value: Number(stat[3]) });
  }
  if (!stats.length) return null;

  return {
    stars: header[1],
    rarity: header[2].toLowerCase(),
    species: header[3].toLowerCase(),
    shiny: !!header[4],
    sprite,
    name: nameLine[1],
    personality: nameLine[2],
    stats,
  };
}

/** /buddy 命令结果卡片。 */
export function BuddyCard({ card }: { card: ParsedBuddyCard }) {
  const color = rarityColors[card.rarity] ?? theme.info;
  return (
    <Pane color={color}>
      <Box flexDirection="column">
        <Box>
          <Text color={color} bold>Buddy </Text>
          <Text bold>{card.name}</Text>
          <Text dimColor>  {card.stars} {card.rarity} {card.species}</Text>
          {card.shiny ? <Text color={theme.warning}> shiny</Text> : null}
        </Box>
        <Box flexDirection="column" marginTop={1}>
          {card.sprite.map((line, idx) => (
            <Text key={`${idx}-${line}`} color={color}>
              {line}
            </Text>
          ))}
        </Box>
        <Box marginTop={1}>
          <Text dimColor>{card.personality}</Text>
        </Box>
        <Box flexDirection="column" marginTop={1}>
          {card.stats.map((stat) => (
            <Box key={stat.name}>
              <Text color={theme.suggestion}>{stat.name.padEnd(10)}</Text>
              <Text> </Text>
              <Text color={color}>{stat.bar}</Text>
              <Text dimColor> {String(stat.value).padStart(3)}</Text>
            </Box>
          ))}
        </Box>
      </Box>
    </Pane>
  );
}
