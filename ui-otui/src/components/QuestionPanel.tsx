/**
 * QuestionPanel：渲染 AskUserQuestionTool 的问答面板（M7）。
 *
 * 在对话流里以 role="ask_question" item 出现。pending（未作答且是当前 active 问题）时
 * 接受键盘输入：
 *   - 单选：↑/↓ 高亮，Enter 选中；最后一项 Other 选中后进自定义输入模式（用 <input>）。
 *   - 多选：↑/↓ 高亮，Space 勾选/取消，Enter 提交勾选集合（不含 Other）。
 *   - Esc：取消。
 * 已作答时只渲染静态摘要。
 *
 * 键盘监听用 useKeyboard，仅当本 item 是 state.activeQuestionId 时才响应。
 */

import { createMemo, createSignal, For, Show } from "solid-js";
import { useKeyboard } from "@opentui/solid";
import type { KeyEvent } from "@opentui/core";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import type { AskQuestionOption, ChatItem } from "../types.js";
import { textAttributes } from "../theme.js";

const OTHER_LABEL = "Other";

export function QuestionPanel(props: { item: ChatItem }) {
  const theme = useTheme();
  const { state, answerQuestion } = useSession();
  const item = () => props.item;

  const pending = createMemo(
    () => !item().answered && state.activeQuestionId === item().questionId,
  );
  const multi = () => !!item().multiSelect;

  // 单选追加 Other 行（除非 allowOther=false）；多选不加
  const rows = createMemo<AskQuestionOption[]>(() => {
    const opts = item().options ?? [];
    if (!multi() && item().allowOther !== false) {
      return [...opts, { label: OTHER_LABEL, description: "自定义答案" }];
    }
    return opts;
  });

  const initialHighlight = () => {
    const r = item().recommendedIndex;
    return typeof r === "number" && r >= 0 && r < rows().length ? r : 0;
  };

  const [highlight, setHighlight] = createSignal(initialHighlight());
  const [checked, setChecked] = createSignal<Set<number>>(new Set());
  const [otherMode, setOtherMode] = createSignal(false);
  const [otherText, setOtherText] = createSignal("");
  let otherInputRef: { value: string } | undefined;

  useKeyboard((key: KeyEvent) => {
    if (!pending()) return;

    if (otherMode()) {
      if (key.name === "escape") {
        key.preventDefault?.();
        setOtherMode(false);
        setOtherText("");
        return;
      }
      if (key.name === "return" || key.name === "enter") {
        const text = otherText().trim();
        if (!text) return;
        key.preventDefault?.();
        answerQuestion(item().questionId!, { selected_labels: [OTHER_LABEL], other_text: text });
        return;
      }
      return; // 其余键交给 <input> 自己处理
    }

    if (key.name === "escape") {
      key.preventDefault?.();
      answerQuestion(item().questionId!, { selected_labels: [], cancelled: true });
      return;
    }
    if (key.name === "up") {
      key.preventDefault?.();
      setHighlight((i) => Math.max(0, i - 1));
      return;
    }
    if (key.name === "down") {
      key.preventDefault?.();
      setHighlight((i) => Math.min(rows().length - 1, i + 1));
      return;
    }
    if (multi() && key.name === "space") {
      key.preventDefault?.();
      setChecked((prev) => {
        const next = new Set(prev);
        const h = highlight();
        if (next.has(h)) next.delete(h);
        else next.add(h);
        return next;
      });
      return;
    }
    if (key.name === "return" || key.name === "enter") {
      key.preventDefault?.();
      if (multi()) {
        const labels = Array.from(checked())
          .sort((a, b) => a - b)
          .map((i) => rows()[i]!.label);
        if (!labels.length) return;
        answerQuestion(item().questionId!, { selected_labels: labels });
        return;
      }
      const sel = rows()[highlight()];
      if (!sel) return;
      if (sel.label === OTHER_LABEL) {
        setOtherMode(true);
        return;
      }
      answerQuestion(item().questionId!, { selected_labels: [sel.label] });
    }
  });

  return (
    <Show
      when={pending()}
      fallback={
        // 静态摘要（已作答）
        <box flexDirection="row" marginTop={1}>
          <box width={2} flexShrink={0}>
            <text fg={theme.text} attributes={textAttributes.muted}>• </text>
          </box>
          <box flexDirection="column" flexGrow={1} minWidth={0}>
            <text fg={theme.text} attributes={textAttributes.muted}>问题：{item().question}</text>
            <Show
              when={!item().answerCancelled}
              fallback={<text fg={theme.warning}>  已取消</text>}
            >
              <text fg={theme.success}>  选择：{(item().answerLabels ?? []).join(", ") || "(空)"}</text>
              <Show when={item().answerOther}>
                <text fg={theme.text} attributes={textAttributes.muted}>  自定义：{item().answerOther}</text>
              </Show>
            </Show>
          </box>
        </box>
      }
    >
      {/* 进行中的问询固定在编辑器上方，只保留一条轻量分隔线。 */}
      <box
        flexDirection="column"
        flexShrink={0}
        marginTop={1}
        marginBottom={1}
        border={["top"]}
        borderColor={theme.border}
        paddingLeft={1}
        paddingRight={1}
      >
        <text fg={theme.text}>
          <b>{item().question}</b>
        </text>
        <text fg={theme.text} attributes={textAttributes.muted}>
          {multi()
            ? "↑/↓ 选择，Space 勾选/取消，Enter 提交，Esc 取消"
            : "↑/↓ 选择，Enter 选中，Esc 取消"}
        </text>
        <For each={rows()}>
          {(opt, i) => {
            const active = () => i() === highlight();
            const isRec = () =>
              typeof item().recommendedIndex === "number" && i() === item().recommendedIndex;
            const isChecked = () => multi() && checked().has(i());
            const marker = () =>
              multi() ? (isChecked() ? "[x]" : "[ ]") : active() ? "›" : " ";
            return (
              <text
                fg={active() ? theme.suggestion : theme.text}
                attributes={active() ? textAttributes.selected : undefined}
              >
                {marker()} {i() + 1}. {opt.label}
                {isRec() ? "  推荐" : ""}
                <Show when={opt.description}>
                  <span style={{ fg: theme.text, attributes: textAttributes.muted }}>{`  ${opt.description}`}</span>
                </Show>
              </text>
            );
          }}
        </For>
        <Show when={otherMode()}>
          <box flexDirection="row" marginTop={1}>
            <box width={2} flexShrink={0}>
              <text fg={theme.primary} attributes={textAttributes.selected}>› </text>
            </box>
            <input
              flexGrow={1}
              minWidth={0}
              focused={true}
              placeholder="自定义答案，Enter 提交，Esc 返回"
              placeholderColor={theme.textMuted}
              textColor={theme.text}
              cursorColor={theme.primary}
              onInput={(v: string) => setOtherText(v)}
              ref={(r: { value: string }) => (otherInputRef = r)}
            />
          </box>
        </Show>
      </box>
    </Show>
  );
}
