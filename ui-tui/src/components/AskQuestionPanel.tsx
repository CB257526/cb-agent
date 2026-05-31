/**
 * AskQuestionPanel：渲染 AskUserQuestionTool 的问答面板。
 *
 * 在 chat item 流里以一个独立的 chat item（role: "ask_question"）出现。
 * pending=true 时接受键盘输入：
 *   - 单选：↑/↓ 切换高亮，Enter 选中并提交；最后一项是 "Other"，选中后弹输入框
 *           填自定义文本，Enter 二次确认提交。
 *   - 多选：↑/↓ 切换高亮，Space 勾选/取消，Enter 提交当前勾选集合。多选不带 Other。
 *   - Esc：取消（cancelled=true）
 *
 * pending=false 时只渲染静态摘要：选中的标签 + 可选 other_text，不再接受输入。
 *
 * 推荐项：recommended_index 对应的项标记 ★，便于用户快速看到模型建议。
 *
 * 视觉风格对齐 SlashCommandPicker：圆角边框 + suggestion 主色高亮。
 */

import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useInput } from "ink";
import { theme } from "../theme.js";
import { AskQuestionOption, ChatItem } from "../types.js";

interface Props {
  item: ChatItem;
  /** pending（未作答）时由 App 传入。已作答时传 undefined，组件转纯展示。 */
  onAnswer?: (params: { selected_labels: string[]; other_text?: string; cancelled?: boolean }) => void;
}

const OTHER_LABEL = "Other";

export function AskQuestionPanel({ item, onAnswer }: Props) {
  const pending = !item.answered;
  const options: AskQuestionOption[] = item.options ?? [];
  const multi = !!item.multiSelect;
  // 单选模式追加一个 Other；多选不加（让模型 multi_select=false 时再问开放题）
  const rows: AskQuestionOption[] = useMemo(() => {
    if (!multi && (item.allowOther !== false)) {
      return [...options, { label: OTHER_LABEL, description: "自定义答案" }];
    }
    return options;
  }, [options, multi, item.allowOther]);

  const [highlight, setHighlight] = useState<number>(() => {
    // 推荐项优先；否则 0
    const r = item.recommendedIndex;
    return typeof r === "number" && r >= 0 && r < rows.length ? r : 0;
  });
  const [checked, setChecked] = useState<Set<number>>(() => new Set());
  const [otherMode, setOtherMode] = useState<boolean>(false);
  const [otherText, setOtherText] = useState<string>("");

  // pending → false 后冻结高亮，不再响应输入
  useInput(
    (input, key) => {
      if (!pending || !onAnswer) return;

      if (otherMode) {
        if (key.escape) {
          setOtherMode(false);
          setOtherText("");
          return;
        }
        if (key.return) {
          const text = otherText.trim();
          if (!text) return;
          onAnswer({ selected_labels: [OTHER_LABEL], other_text: text });
          return;
        }
        if (key.backspace || key.delete) {
          setOtherText((s) => s.slice(0, -1));
          return;
        }
        if (input && !key.ctrl && !key.meta) {
          setOtherText((s) => s + input);
        }
        return;
      }

      if (key.escape) {
        onAnswer({ selected_labels: [], cancelled: true });
        return;
      }
      if (key.upArrow) {
        setHighlight((i) => Math.max(0, i - 1));
        return;
      }
      if (key.downArrow) {
        setHighlight((i) => Math.min(rows.length - 1, i + 1));
        return;
      }
      if (multi && input === " ") {
        setChecked((prev) => {
          const next = new Set(prev);
          if (next.has(highlight)) next.delete(highlight);
          else next.add(highlight);
          return next;
        });
        return;
      }
      if (key.return) {
        if (multi) {
          const labels = Array.from(checked).sort((a, b) => a - b).map((i) => rows[i]!.label);
          if (!labels.length) return;  // 空提交无意义，等用户至少勾一个
          onAnswer({ selected_labels: labels });
          return;
        }
        // 单选
        const sel = rows[highlight];
        if (!sel) return;
        if (sel.label === OTHER_LABEL) {
          setOtherMode(true);
          return;
        }
        onAnswer({ selected_labels: [sel.label] });
      }
    },
    { isActive: pending && !!onAnswer },
  );

  // 已作答：渲染摘要
  if (!pending) {
    const labels = item.answerLabels ?? [];
    const cancelled = !!item.answerCancelled;
    return (
      <Box borderStyle="round" borderColor={theme.border} paddingX={1} flexDirection="column">
        <Text dimColor>问题：{item.question}</Text>
        {cancelled ? (
          <Text color={theme.warning}>· 已取消</Text>
        ) : (
          <Box flexDirection="column">
            <Text color={theme.success}>· 选择：{labels.join(", ") || "(空)"}</Text>
            {item.answerOther ? (
              <Text dimColor>  自定义：{item.answerOther}</Text>
            ) : null}
          </Box>
        )}
      </Box>
    );
  }

  // 进行中
  const recIdx = item.recommendedIndex;
  return (
    <Box borderStyle="round" borderColor={theme.suggestion} paddingX={1} flexDirection="column">
      <Text bold>{item.question}</Text>
      <Text dimColor>
        {multi
          ? "↑/↓ 选择，Space 勾选/取消，Enter 提交，Esc 取消"
          : "↑/↓ 选择，Enter 选中，Esc 取消"}
      </Text>
      {rows.map((opt, i) => {
        const active = i === highlight;
        const isRec = typeof recIdx === "number" && i === recIdx;
        const isChecked = multi && checked.has(i);
        const marker = multi ? (isChecked ? "[x]" : "[ ]") : (active ? "▸" : " ");
        return (
          <Box key={opt.label + i}>
            <Text color={active ? theme.suggestion : undefined} bold={active}>
              {marker} {opt.label}
              {isRec ? " ★" : ""}
            </Text>
            {opt.description ? (
              <Text dimColor={!active}>  — {opt.description}</Text>
            ) : null}
          </Box>
        );
      })}
      {otherMode ? (
        <Box marginTop={1} borderStyle="single" borderColor={theme.borderActive} paddingX={1}>
          <Text>自定义：</Text>
          <Text>{otherText}</Text>
          <Text inverse> </Text>
          <Text dimColor>  (Enter 提交，Esc 返回)</Text>
        </Box>
      ) : null}
    </Box>
  );
}
