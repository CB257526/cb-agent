import React, { useEffect, useState } from "react";
import { Box, Text, useInput } from "ink";
import { theme } from "../theme.js";
import { SlashCommand, filterCommands } from "../commands.js";

interface Props {
  /** 不带 '/' 的查询字符串（输入框去掉前缀后的部分） */
  query: string;
  /** 用户选了一项 */
  onSelect: (cmd: SlashCommand) => void;
  /** 用户取消（Esc 或删光了 '/'） */
  onCancel: () => void;
}

/**
 * / 命令选择浮层。
 *
 * 出现时机：用户在空输入框首字符输入 '/' → App 设 active=true，渲染本组件覆盖在
 *   PromptInput 上方。
 *
 * 交互：
 *   - ↑/↓ 切换高亮项（PromptInput 这时不响应方向键，因为 disabled=true）
 *   - Enter 选中
 *   - Esc 取消
 *   - 输入字符在 PromptInput 里继续打，本组件实时按 prefix 过滤
 */
export function SlashCommandPicker({ query, onSelect, onCancel }: Props) {
  const [selected, setSelected] = useState(0);
  const filtered = filterCommands(query);

  // query 变化时重置高亮位
  useEffect(() => { setSelected(0); }, [query]);

  useInput((_, key) => {
    if (key.escape) { onCancel(); return; }
    if (key.upArrow) { setSelected((i) => Math.max(0, i - 1)); return; }
    if (key.downArrow) { setSelected((i) => Math.min(filtered.length - 1, i + 1)); return; }
    if (key.return) {
      const cmd = filtered[selected];
      if (cmd) onSelect(cmd);
      return;
    }
  });

  if (!filtered.length) {
    return (
      <Box borderStyle="round" borderColor={theme.border} paddingX={1} flexDirection="column">
        <Text dimColor>未找到匹配命令（Esc 取消）</Text>
      </Box>
    );
  }

  return (
    <Box borderStyle="round" borderColor={theme.suggestion} paddingX={1} flexDirection="column">
      <Text dimColor>命令（↑/↓ 选择，Enter 执行，Esc 取消）</Text>
      {filtered.map((cmd, i) => {
        const active = i === selected;
        return (
          <Box key={cmd.name}>
            <Text color={active ? theme.suggestion : undefined} bold={active}>
              {active ? "▸ " : "  "}{cmd.name.padEnd(10)}
            </Text>
            <Text dimColor={!active}>{cmd.description}</Text>
          </Box>
        );
      })}
    </Box>
  );
}
