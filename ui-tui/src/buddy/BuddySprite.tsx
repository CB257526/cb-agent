import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import type { BuddyCompanion, BuddyState } from "../types.js";

const NARROW_COLUMNS = 90;
const PET_BURST_MS = 2500;
const BUBBLE_SHOW_MS = 10000;

const rarityColors: Record<string, string> = {
  common: theme.textMuted,
  uncommon: theme.success,
  rare: theme.info,
  epic: theme.agent,
  legendary: theme.warning,
};

export interface BuddySpriteProps {
  state: BuddyState | null;
  columns?: number;
}

type VisibleBuddyState = BuddyState & { companion: BuddyCompanion };

/** 输入框旁的 Buddy 附属视图。 */
export function BuddySprite({ state, columns = process.stdout.columns ?? 120 }: BuddySpriteProps) {
  if (!shouldRenderBuddy(state)) return null;

  const companion = state.companion;
  const color = rarityColors[companion.rarity] ?? theme.info;
  const now = Date.now();
  const petting = typeof state.pet_at === "number" && now - state.pet_at < PET_BURST_MS;
  const speaking = !!state.last_reaction
    && typeof state.reaction_at === "number"
    && now - state.reaction_at < BUBBLE_SHOW_MS;

  if (isNarrowBuddyLayout(columns)) {
    const quip = speaking ? clip(state.last_reaction ?? "", 24) : companion.name;
    return (
      <Box paddingX={1}>
        <Text>
          {petting ? <Text color={theme.agent}>{"<3 "}</Text> : null}
          <Text bold color={color}>{companion.face}</Text>{" "}
          <Text dimColor={!speaking} italic={speaking} color={speaking ? color : undefined}>
            {quip}
          </Text>
        </Text>
      </Box>
    );
  }

  // 终端里的任何定时重绘都会干扰 scrollback、鼠标滚轮和文本选择。Buddy 在空闲
  // 状态下固定使用第一帧，只在后端事件或用户输入导致 App 自然刷新时更新视图。
  const frame = selectStaticBuddyFrame(companion);
  const spriteLines = petting ? ["   <3  <3   ", ...frame] : frame;

  return (
    <Box flexDirection="row" alignItems="flex-end" paddingX={1}>
      {speaking ? <SpeechBubble text={state.last_reaction ?? ""} color={color} /> : null}
      <Box flexDirection="column" alignItems="center" flexShrink={0}>
        {spriteLines.map((line, i) => (
          <Text key={i} color={i === 0 && petting ? theme.agent : color}>
            {line}
          </Text>
        ))}
        <Text italic dimColor={!speaking} color={speaking ? color : undefined}>
          {companion.name}
        </Text>
      </Box>
    </Box>
  );
}

export function shouldRenderBuddy(state: BuddyState | null): state is VisibleBuddyState {
  return !!state?.enabled && !state.muted && !!state.companion;
}

export function isNarrowBuddyLayout(columns: number): boolean {
  return columns < NARROW_COLUMNS;
}

export function selectStaticBuddyFrame(companion: BuddyCompanion): string[] {
  const frames = companion.frames?.length ? companion.frames : [companion.sprite ?? []];
  return frames[0] ?? [];
}

function SpeechBubble({ text, color }: { text: string; color: string }) {
  const lines = wrap(text, 28);
  return (
    <Box flexDirection="row" alignItems="center" marginRight={1}>
      <Box borderStyle="round" borderColor={color} paddingX={1} flexDirection="column">
        {lines.map((line, i) => (
          <Text key={i} italic dimColor>
            {line}
          </Text>
        ))}
      </Box>
      <Text color={color}>-</Text>
    </Box>
  );
}

function wrap(text: string, width: number): string[] {
  const words = String(text || "").split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let current = "";
  for (const word of words) {
    if (current && current.length + word.length + 1 > width) {
      lines.push(current);
      current = word;
    } else {
      current = current ? `${current} ${word}` : word;
    }
  }
  if (current) lines.push(current);
  return lines.length ? lines : [""];
}

function clip(text: string, max: number): string {
  if (text.length <= max) return text;
  return `${text.slice(0, Math.max(0, max - 1))}…`;
}
