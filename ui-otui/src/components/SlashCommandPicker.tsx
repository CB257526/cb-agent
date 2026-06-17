/**
 * SlashCommandPicker：输入 "/" 前缀时弹出的命令选择面板。
 *
 * 只在输入"纯命令名前缀"（无空格、无参数）时显示。↑/↓ 选择，Enter 执行，Esc 关闭。
 * 带参数的命令（如 /switch <id>）让 Prompt 的 Enter 直接走 submit，不被本面板拦截。
 */

import { For, createSignal, createEffect } from "solid-js";
import { useTheme } from "../context/theme.js";
import { filterCommands, type SlashCommand } from "../commands.js";

export function SlashCommandPicker(props: {
  query: string;
  selectedIndex: number;
}) {
  const theme = useTheme();
  const matches = () => filterCommands(props.query);

  return (
    <box
      flexDirection="column"
      border
      borderColor={theme.border}
      backgroundColor={theme.backgroundPanel}
      paddingLeft={1}
      paddingRight={1}
    >
      <For each={matches()}>
        {(cmd: SlashCommand, i) => {
          const active = () => i() === props.selectedIndex;
          return (
            <text fg={active() ? theme.background : theme.text} bg={active() ? theme.suggestion : undefined}>
              {active() ? "▶ " : "  "}
              <b>{cmd.name}</b>
              <span style={{ fg: active() ? theme.background : theme.textMuted }}>
                {"  "}
                {cmd.description}
              </span>
            </text>
          );
        }}
      </For>
    </box>
  );
}
