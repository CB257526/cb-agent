/**
 * Prompt（M5）：单行输入框 + Enter 提交 + slash 命令面板 + ↑/↓ 历史导航。
 *
 * OpenTUI 内置 <input> 原生处理退格/长按 delete/光标移动（旧 Ink 版长按 delete 失灵的
 * 根治点）。本组件在其之上叠加：
 *   - slash 面板：输入以 "/" 开头且无空格时弹出，↑/↓ 选命令、Enter 执行、Esc 关闭
 *   - 历史导航：输入框为空时 ↑/↓ 翻 historyStore（命令面板激活时让位给面板）
 *
 * 键盘拦截用 useKeyboard 全局监听；命令面板/历史导航激活时 preventDefault 掉方向键，
 * 避免 <input> 自己消费。
 */

import { createSignal, createMemo, Show } from "solid-js";
import { useKeyboard } from "@opentui/solid";
import type { KeyEvent, PasteEvent } from "@opentui/core";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import { filterCommands } from "../commands.js";
import { SlashCommandPicker } from "./SlashCommandPicker.js";
import { AttachmentQueue } from "./AttachmentQueue.js";

export function Prompt() {
  const theme = useTheme();
  const { state, submit, runCommand, getHistoryAt, pasteFromClipboard, togglePlanMode } = useSession();
  const [value, setValue] = createSignal("");
  const [pickerIndex, setPickerIndex] = createSignal(0);
  const [historyIdx, setHistoryIdx] = createSignal(-1);

  let inputRef: { value: string } | undefined;
  // 程序化改值（翻历史 / 粘贴）时置 true：让 onInput 跳过"重置 historyIdx"逻辑。
  // 否则 setInputValue 写 inputRef.value 会触发 Input 的 input 事件，把刚设好的
  // historyIdx 又重置成 -1，导致方向键只能翻一次就失灵。
  let programmatic = false;

  // 问答进行中 / 弹窗打开时禁用输入：让位给 QuestionPanel / SelectDialog 的键盘处理
  const disabled = createMemo(() => state.busy || state.activeQuestionId !== null || state.dialog !== null);

  const setInputValue = (v: string) => {
    programmatic = true;
    setValue(v);
    if (inputRef) inputRef.value = v;
    programmatic = false;
  };

  // slash 面板：以 "/" 开头、无空格、未禁用时激活
  const slashActive = createMemo(
    () => value().startsWith("/") && !value().trim().includes(" ") && !disabled(),
  );

  const handleSubmit = () => {
    const text = value();
    // 文本为空但有附件时也允许提交（submit 内部会拼"请根据附件回答"）
    if (disabled()) return;
    if (!text.trim() && state.attachments.length === 0) return;
    submit(text);
    setInputValue("");
    setHistoryIdx(-1);
  };

  // 粘贴处理：真实终端的 Ctrl-V 会被解析成"括号粘贴"事件（onPaste），而不是
  // ctrl+v 按键——所以之前监听 key.name==="v" 在真实终端永远收不到，只有
  // 直接喂 \x16 字节的探针能触发（导致误判）。
  //
  // 文本粘贴：event.bytes 解码出文本，input 默认行为会自动插入，这里不拦截。
  // 图片/文件粘贴：Windows Terminal 发来的是「空的」括号粘贴（bytes 为空或全空白），
  // 此时主动去读系统剪贴板，把图片/文件加入附件队列。
  const handlePaste = (event: PasteEvent) => {
    if (disabled()) {
      event.preventDefault?.();
      return;
    }
    const text = new TextDecoder().decode(event.bytes ?? new Uint8Array());
    if (text.trim()) {
      // 有文本：让 input 走默认插入行为，不拦截
      return;
    }
    // 空括号粘贴 = 剪贴板里是图片/文件。拦掉默认行为，主动读系统剪贴板。
    event.preventDefault?.();
    pasteFromClipboard((t) => {
      if (!t) return;
      setInputValue(value() + t);
    });
  };

  useKeyboard((key: KeyEvent) => {
    if (disabled()) return;

    if (key.name === "tab" && !slashActive()) {
      key.preventDefault?.();
      togglePlanMode();
      return;
    }

    // slash 面板激活：方向键选命令、Enter 执行、Esc 关闭
    if (slashActive()) {
      const matches = filterCommands(value().slice(1));
      if (key.name === "up") {
        key.preventDefault?.();
        setPickerIndex((i) => Math.max(0, i - 1));
        return;
      }
      if (key.name === "down") {
        key.preventDefault?.();
        setPickerIndex((i) => Math.min(Math.max(0, matches.length - 1), i + 1));
        return;
      }
      if (key.name === "return" || key.name === "enter") {
        const cmd = matches[pickerIndex()];
        if (cmd) {
          key.preventDefault?.();
          runCommand(cmd, cmd.name);
          setInputValue("");
          setPickerIndex(0);
          return;
        }
      }
      if (key.name === "escape") {
        key.preventDefault?.();
        setInputValue("");
        setPickerIndex(0);
        return;
      }
      return;
    }

    // 输入框为空时 ↑/↓ 翻历史
    if (value() === "" || historyIdx() >= 0) {
      if (key.name === "up") {
        const next = historyIdx() + 1;
        const text = getHistoryAt(next);
        if (text !== null) {
          key.preventDefault?.();
          setHistoryIdx(next);
          setInputValue(text);
        }
        return;
      }
      if (key.name === "down") {
        const next = historyIdx() - 1;
        if (next < 0) {
          key.preventDefault?.();
          setHistoryIdx(-1);
          setInputValue("");
        } else {
          const text = getHistoryAt(next);
          if (text !== null) {
            key.preventDefault?.();
            setHistoryIdx(next);
            setInputValue(text);
          }
        }
        return;
      }
    }
  });

  return (
    <box flexDirection="column" flexShrink={0}>
      <AttachmentQueue />
      <Show when={slashActive()}>
        <SlashCommandPicker query={value().slice(1)} selectedIndex={pickerIndex()} />
      </Show>
      <box border borderColor={theme.borderActive} paddingLeft={1} paddingRight={1}>
        <input
          focused={!disabled()}
          placeholder={
            state.activeQuestionId !== null
              ? "请在上方作答…"
              : state.busy
                ? "agent 正在工作…"
                : state.planState?.mode === "plan"
                  ? "Plan Mode: ask for a plan or review..."
                  : "输入消息，回车发送（/ 看命令）"
          }
          placeholderColor={theme.textMuted}
          textColor={theme.text}
          cursorColor={theme.primary}
          onInput={(v: string) => {
            setValue(v);
            // 程序化改值（翻历史/粘贴）不算用户键入，不重置历史索引
            if (programmatic) return;
            setHistoryIdx(-1);
            setPickerIndex(0);
          }}
          onPaste={handlePaste}
          onSubmit={handleSubmit}
          ref={(r: { value: string }) => (inputRef = r)}
        />
      </box>
    </box>
  );
}
