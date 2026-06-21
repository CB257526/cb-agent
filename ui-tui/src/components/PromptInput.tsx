/**
 * 自写的轻量受控输入框。
 *
 * 替换 `ink-text-input` 的原因：
 *   1) ink-text-input 不暴露光标位置，没法做 inverse 高亮当前字符
 *   2) ↑/↓ 翻历史需要拦截方向键，ink-text-input 内部把它们吃掉
 *
 * 不支持：多行（Enter = 提交）、IME、双宽字符的精确光标定位、鼠标。
 *
 * 历史翻页规则：
 *   - 输入框非空 → 不响应 ↑/↓（避免误触盖掉正在打的字）
 *   - 输入框空 → ↑ 进入"翻历史"模式，从最后一条往前；↓ 往后；走到边界停在边界
 *   - 翻历史时按字符键退出翻历史模式（保留当前显示文本，光标移到末尾）
 */

import React, { useState, useRef, useEffect } from "react";
import { Box, Text, useInput } from "ink";
import { theme } from "../theme.js";
import type { PlanMode } from "../types.js";

export interface PromptInputProps {
  value: string;
  onChange: (v: string) => void;
  onSubmit: (v: string) => void;
  onPasteRequest?: (insertText: (text: string) => void) => void;
  disabled: boolean;
  mode?: PlanMode;
  /** 历史读取器：null 表示无更多 / 边界。idx 0=最新一条，递增=更老 */
  getHistoryAt?: (idx: number) => string | null;
  /** 浮层（如 SlashCommandPicker）激活时设 true：拦截 ↑/↓/Enter/Esc 给浮层处理，
   *  本输入框只处理字符编辑（继续打字 / Backspace / 光标移动） */
  delegateNavKeys?: boolean;
}

export function PromptInput({ value, onChange, onSubmit, onPasteRequest, disabled, mode = "execute", getHistoryAt, delegateNavKeys }: PromptInputProps) {
  const [cursor, setCursor] = useState(value.length);
  const [historyIdx, setHistoryIdx] = useState<number | null>(null);
  const valueRef = useRef(value);
  const cursorRef = useRef(cursor);

  // 父组件外部改 value 时（比如选了命令后清空），把光标拉回末尾
  const lastValue = useRef(value);
  useEffect(() => {
    if (lastValue.current !== value) {
      setCursor(value.length);
      lastValue.current = value;
    }
    valueRef.current = value;
  }, [value]);
  useEffect(() => {
    cursorRef.current = cursor;
  }, [cursor]);

  useInput((input, key) => {
    if (disabled) return;
    if (key.tab) return;

    // 浮层激活时：上/下/回车/Esc 让给浮层；字符编辑继续在这里处理
    if (delegateNavKeys && (key.upArrow || key.downArrow || key.return || key.escape)) {
      return;
    }

    // ── 历史翻页 ──
    if (key.upArrow) {
      if (value !== "" && historyIdx === null) return;  // 防误触
      if (!getHistoryAt) return;
      const next = (historyIdx ?? -1) + 1;
      const text = getHistoryAt(next);
      if (text !== null) {
        setHistoryIdx(next);
        onChange(text);
      }
      return;
    }
    if (key.downArrow) {
      if (historyIdx === null) return;
      if (!getHistoryAt) return;
      const next = historyIdx - 1;
      if (next < 0) {
        setHistoryIdx(null);
        onChange("");
      } else {
        const text = getHistoryAt(next);
        if (text !== null) {
          setHistoryIdx(next);
          onChange(text);
        }
      }
      return;
    }

    // 任何编辑动作都退出翻历史模式
    if (historyIdx !== null) setHistoryIdx(null);

    // ── 编辑动作 ──
    if (key.ctrl && (input === "v" || input === "\u0016")) {
      onPasteRequest?.((text) => {
        if (!text) return;
        const current = valueRef.current;
        const currentCursor = cursorRef.current;
        const next = current.slice(0, currentCursor) + text + current.slice(currentCursor);
        const nextCursor = currentCursor + text.length;
        lastValue.current = next;
        valueRef.current = next;
        cursorRef.current = nextCursor;
        onChange(next);
        setCursor(nextCursor);
      });
      return;
    }
    if (key.return) {
      onSubmit(value);
      setCursor(0);
      return;
    }
    if (key.leftArrow) {
      setCursor((c) => Math.max(0, c - 1));
      return;
    }
    if (key.rightArrow) {
      setCursor((c) => Math.min(value.length, c + 1));
      return;
    }
    if (key.backspace || key.delete) {
      // ink 把 Backspace 报成 backspace=true，Delete 报成 delete=true
      // Windows 上 Backspace 经常被报成 delete=true，所以两者都视作"删左边一个字符"
      if (cursor === 0) return;
      const next = value.slice(0, cursor - 1) + value.slice(cursor);
      onChange(next);
      setCursor(cursor - 1);
      return;
    }
    if (key.ctrl && input === "a") {
      setCursor(0);
      return;
    }
    if (key.ctrl && input === "e") {
      setCursor(value.length);
      return;
    }
    if (key.ctrl && input === "u") {
      onChange(value.slice(cursor));
      setCursor(0);
      return;
    }
    // 普通可打印字符（含中文，会一次塞多字节但显示宽度可能不准——已知限制）
    if (input && !key.ctrl && !key.meta) {
      const next = value.slice(0, cursor) + input + value.slice(cursor);
      onChange(next);
      setCursor(cursor + input.length);
    }
  });

  return (
    <Box
      borderStyle="round"
      borderColor={disabled ? theme.border : theme.borderActive}
      flexGrow={1}
      paddingX={1}
      width="100%"
    >
      <Text color={disabled ? theme.border : theme.primary}>{"> "}</Text>
      {disabled
        ? <Text dimColor>（agent 正在工作，等待结束）</Text>
        : <CursorText value={value} cursor={cursor} placeholder={mode === "plan" ? "Plan Mode: ask for a plan or review..." : "跟 cb-agent 说点什么…"} />}
    </Box>
  );
}

/** 用 inverse 高亮模拟终端光标位置。空输入框时高亮 placeholder 首字符。 */
function CursorText({ value, cursor, placeholder }: { value: string; cursor: number; placeholder: string }) {
  if (value === "") {
    return (
      <Text>
        <Text inverse>{placeholder.charAt(0)}</Text>
        <Text dimColor>{placeholder.slice(1)}</Text>
      </Text>
    );
  }
  const before = value.slice(0, cursor);
  const at = cursor < value.length ? value[cursor] : " ";
  const after = cursor < value.length ? value.slice(cursor + 1) : "";
  return (
    <Text>
      {before}
      <Text inverse>{at}</Text>
      {after}
    </Text>
  );
}
