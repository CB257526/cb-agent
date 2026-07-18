/**
 * SlashCommandPicker：输入 "/" 前缀时弹出的命令选择面板。
 *
 * 只在输入"纯命令名前缀"（无空格、无参数）时显示。↑/↓ 选择，Enter 执行，Esc 关闭。
 * 带参数的命令（如 /switch <id>）让 Prompt 的 Enter 直接走 submit，不被本面板拦截。
 */

import { For, Show, createMemo } from "solid-js";
import { useTheme } from "../context/theme.js";
import { filterCommands, type SlashCommand } from "../commands.js";
import { sliceAroundSelection } from "../layout.js";
import { textAttributes } from "../theme.js";

const MAX_VISIBLE_COMMANDS = 8;

export function SlashCommandPicker(props: {
  query: string;
  selectedIndex: number;
}) {
  const theme = useTheme();
  const matches = () => filterCommands(props.query);
  const visible = createMemo(() =>
    sliceAroundSelection(matches(), props.selectedIndex, MAX_VISIBLE_COMMANDS),
  );

  return (
    <box flexDirection="column" paddingLeft={2} marginBottom={1}>
      <Show
        when={visible().items.length > 0}
        fallback={<text fg={theme.text} attributes={textAttributes.muted}>没有匹配的命令</text>}
      >
        <For each={visible().items}>
          {(cmd: SlashCommand, i) => {
            const absoluteIndex = () => visible().start + i();
            const active = () => absoluteIndex() === props.selectedIndex;
            return (
              <text
                fg={active() ? theme.suggestion : theme.text}
                attributes={active() ? textAttributes.selected : undefined}
                wrapMode="none"
                truncate
              >
                {active() ? "› " : "  "}
                {cmd.name}
                <span style={{ fg: theme.text, attributes: textAttributes.muted }}>
                  {`  ${cmd.description}`}
                </span>
              </text>
            );
          }}
        </For>
      </Show>
    </box>
  );
}
