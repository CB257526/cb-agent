/**
 * SelectDialog：浮层 Select 弹窗（opencode 风格）。
 *
 * /sessions /tools /mcp 等命令不再往对话流打印长文本，而是开这个居中小窗。
 *
 * 键盘处理为什么自己接管而不靠原生 <select>：
 *   OpenTUI 的 SelectRenderable 只在被焦点系统聚焦时才走 handleKeyPress → emit
 *   "itemSelected"。但弹窗弹出时 Prompt 的 <input> 失焦后，焦点未必自动转交给
 *   <select>，导致 select 收不到回车、itemSelected 永不触发——表现为"选中回车没反应"。
 *   仿 opencode：自己用 useKeyboard 接管 上/下/回车/Esc，<select> 设 focused={false}
 *   纯作受控展示（selectedIndex 由我们驱动），不再依赖它的焦点与事件。
 */

import { createMemo, createSignal, Show } from "solid-js";
import { useKeyboard } from "@opentui/solid";
import type { KeyEvent } from "@opentui/core";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import type { DialogSpec } from "../types.js";

export function SelectDialog(props: { spec: DialogSpec }) {
  const theme = useTheme();
  const { closeDialog } = useSession();

  // OpenTUI select 需要 {name, description, value} 形状的 options
  const options = createMemo(() =>
    props.spec.options.map((o) => ({
      name: o.name,
      description: o.description ?? "",
      value: o.value,
    })),
  );

  // 当前高亮项索引；上/下键驱动，回车按它取值。
  const [index, setIndex] = createSignal(0);

  const commit = () => {
    // 必须在 closeDialog() 之前把 onSelect 和 value 抓出来：
    // props.spec 是 Solid 响应式 getter，读的是 state.dialog；closeDialog() 把
    // state.dialog 置 null 后，props.spec 立刻变 null，再读 props.spec.onSelect
    // 就成了 null?.()，回调被静默跳过——这正是"选中回车没切换"的根因。
    const picked = props.spec.options[index()];
    const onSelect = props.spec.onSelect;
    closeDialog();
    if (picked && typeof picked.value === "string") onSelect?.(picked.value);
  };

  // 弹窗自己接管全部键：上/下移动（循环）、回车确认、Esc 关闭。
  useKeyboard((key: KeyEvent) => {
    const n = options().length;
    if (key.name === "up") {
      key.preventDefault?.();
      if (n > 0) setIndex((i) => (i - 1 + n) % n);
      return;
    }
    if (key.name === "down") {
      key.preventDefault?.();
      if (n > 0) setIndex((i) => (i + 1) % n);
      return;
    }
    if (key.name === "return" || key.name === "enter" || key.name === "linefeed") {
      key.preventDefault?.();
      commit();
      return;
    }
    if (key.name === "escape") {
      key.preventDefault?.();
      closeDialog();
      return;
    }
  });

  // Select 的可见项数 = floor(height / linesPerItem)。在 flex 容器里 flexGrow 算不出
  // 确定高度，会塌缩成只剩标题。所以这里按选项数显式算高度：showDescription 时每项
  // 占 2 行，最多显示 10 项（再多则 select 自身滚动）。
  const LINES_PER_ITEM = 2;
  const MAX_VISIBLE = 10;
  const listHeight = createMemo(() => {
    const n = Math.min(options().length, MAX_VISIBLE);
    return Math.max(1, n) * LINES_PER_ITEM;
  });

  return (
    <box
      position="absolute"
      top={0}
      left={0}
      right={0}
      bottom={0}
      zIndex={100}
      alignItems="center"
      justifyContent="center"
      backgroundColor={theme.background}
    >
      <box
        flexDirection="column"
        width={64}
        border
        borderColor={theme.borderActive}
        backgroundColor={theme.backgroundPanel}
        paddingLeft={1}
        paddingRight={1}
      >
        {/* 标题 */}
        <box flexShrink={0} paddingBottom={1}>
          <text fg={theme.accent}>
            <b>{props.spec.title}</b>
          </text>
        </box>

        {/* 选项列表 */}
        <Show
          when={options().length > 0}
          fallback={<text fg={theme.textMuted}>（空）</text>}
        >
          <select
            focused={false}
            selectedIndex={index()}
            height={listHeight()}
            options={options()}
            showDescription={true}
            showScrollIndicator={true}
            wrapSelection={true}
            backgroundColor={theme.backgroundPanel}
            textColor={theme.text}
            focusedBackgroundColor={theme.backgroundPanel}
            focusedTextColor={theme.text}
            selectedBackgroundColor={theme.backgroundElement}
            selectedTextColor={theme.borderActive}
            descriptionColor={theme.textMuted}
            selectedDescriptionColor={theme.textMuted}
          />
        </Show>

        {/* 底部提示 */}
        <box flexShrink={0} paddingTop={1}>
          <text fg={theme.textMuted}>↑/↓ 选择 · Enter 确认 · Esc 关闭</text>
        </box>
      </box>
    </box>
  );
}
