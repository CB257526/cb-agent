/**
 * Prompt（M5）：多行输入框 + Enter 提交 + slash 命令面板 + ↑/↓ 历史导航。
 *
 * OpenTUI 内置 <textarea> 原生处理退格/长按 delete/光标移动（旧 Ink 版长按 delete 失灵的
 * 根治点）。本组件在其之上叠加：
 *   - slash 面板：输入以 "/" 开头且无空格时弹出，↑/↓ 选命令、Enter 执行、Esc 关闭
 *   - 历史导航：输入框为空时 ↑/↓ 翻 historyStore（命令面板激活时让位给面板）
 *   - Shift+Enter 换行，长文本按宽度自动换行
 *
 * 键盘拦截用 useKeyboard 全局监听；命令面板/历史导航激活时 preventDefault 掉方向键，
 * 避免 <textarea> 自己消费。
 */

import { createSignal, createMemo, onCleanup, onMount, Show } from "solid-js";
import { wrapAnsi } from "bun";
import { useKeyboard, usePaste, useTerminalDimensions } from "@opentui/solid";
import type { KeyEvent, PasteEvent, TextareaRenderable } from "@opentui/core";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import { filterCommands } from "../commands.js";
import { SlashCommandPicker } from "./SlashCommandPicker.js";
import { AttachmentQueue } from "./AttachmentQueue.js";
import { getPromptMaxHeight } from "../layout.js";
import { textAttributes } from "../theme.js";

type TextareaKeyBinding = {
  name: string;
  ctrl?: boolean;
  shift?: boolean;
  super?: boolean;
  action: "submit" | "newline" | "select-all";
};

export function Prompt() {
  const theme = useTheme();
  const dimensions = useTerminalDimensions();
  const {
    state,
    submit,
    runCommand,
    getHistoryAt,
    pasteFromClipboard,
    togglePlanMode,
    togglePermissionMode,
    registerPromptInputSetter,
  } = useSession();
  const [value, setValue] = createSignal("");
  const [pickerIndex, setPickerIndex] = createSignal(0);
  const [historyIdx, setHistoryIdx] = createSignal(-1);
  const [textareaWidth, setTextareaWidth] = createSignal(0);
  const maxInputHeight = () => getPromptMaxHeight(dimensions().height);

  let inputRef: TextareaRenderable | undefined;
  // 程序化改值（翻历史 / 粘贴）时置 true：让 onInput 跳过"重置 historyIdx"逻辑。
  // 否则 setInputValue 写 textarea 内容会触发 contentChange，把刚设好的
  // historyIdx 又重置成 -1，导致方向键只能翻一次就失灵。
  let programmatic = false;

  // 问答进行中 / 弹窗打开时禁用输入：让位给 QuestionPanel / SelectDialog 的键盘处理
  const disabled = createMemo(() => state.busy || state.activeQuestionId !== null || state.dialog !== null);

  const setInputValue = (v: string) => {
    programmatic = true;
    setValue(v);
    if (inputRef) inputRef.setText(v);
    programmatic = false;
  };

  // 把输入框写入器注册给 session，供 /skill 等命令注入 `$skill` 提及。
  onMount(() => {
    registerPromptInputSetter(setInputValue);
  });
  onCleanup(() => {
    registerPromptInputSetter(null);
  });

  const visualRowsForText = (text: string, width: number) =>
    text
      .split("\n")
      .reduce((sum, line) => {
        if (!line) return sum + 1;
        return sum + Math.max(1, wrapAnsi(line, width, {
          hard: true,
          trim: false,
          wordWrap: true,
        }).split("\n").length);
      }, 0);

  const inputHeight = createMemo(() => {
    const text = value();
    if (!text) return 1;
    const width = Math.max(1, textareaWidth() || dimensions().width - 4);
    // EditorView 给真实渲染行数；wrapAnsi 估算用于删除收缩时兜底，
    // 避免内部 viewport 偶尔晚一拍导致输入框残留空白行。
    const estimatedRows = visualRowsForText(text, width);
    const vlc = inputRef?.editorView.getTotalVirtualLineCount();
    const rows =
      typeof vlc === "number" && Number.isFinite(vlc) && vlc > 0
        ? Math.min(vlc, estimatedRows)
        : estimatedRows;
    return Math.max(1, Math.min(maxInputHeight(), rows));
  });

  const textareaKeyBindings: TextareaKeyBinding[] = [
    { name: "return", action: "submit" },
    { name: "kpenter", action: "submit" },
    { name: "linefeed", action: "submit" },
    { name: "return", shift: true, action: "newline" },
    { name: "kpenter", shift: true, action: "newline" },
    { name: "linefeed", shift: true, action: "newline" },
    // Ctrl+A / Super+A 全选（终端原生只传 Ctrl+A，super 版本给 macOS 用）
    { name: "a", ctrl: true, action: "select-all" },
    { name: "a", super: true, action: "select-all" },
  ];

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

  // 粘贴处理：多数真实终端的 Ctrl-V/Cmd-V 会被解析成 bracketed paste
  // 事件（onPaste），但不同终端/启动方式对剪贴板图片的行为不完全一致；
  // useKeyboard 里仍保留快捷键兜底，直接读取系统剪贴板。
  //
  // 文本粘贴：event.bytes 解码出文本，textarea 默认行为会自动插入，这里不拦截。
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

  usePaste(handlePaste);

  useKeyboard((key: KeyEvent) => {
    if (disabled()) return;

    const keyName = String(key.name || "").toLowerCase();
    const keySequence = String((key as { sequence?: unknown }).sequence || "");
    const isPasteShortcut =
      keyName === "paste"
      || ((key.ctrl || key.super || key.meta) && keyName === "v")
      || keySequence === "\x16";
    if (isPasteShortcut) {
      key.preventDefault?.();
      pasteFromClipboard((t) => {
        if (!t) return;
        setInputValue(value() + t);
      });
      return;
    }

    if (key.name === "tab" && !slashActive()) {
      key.preventDefault?.();
      togglePlanMode();
      return;
    }

    if (key.ctrl && key.name === "r" && !slashActive()) {
      key.preventDefault?.();
      togglePermissionMode();
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
      <box flexDirection="row" marginTop={1} minWidth={0}>
        <box width={2} flexShrink={0}>
          <text
            fg={state.planState?.mode === "plan" ? theme.agent : theme.primary}
            attributes={textAttributes.selected}
          >
            {"› "}
          </text>
        </box>
        <textarea
          flexGrow={1}
          minWidth={0}
          height={inputHeight()}
          maxHeight={maxInputHeight()}
          wrapMode="word"
          focused={!disabled()}
          placeholder={
            state.activeQuestionId !== null
              ? "请在上方作答…"
              : state.busy
                ? "agent 正在工作…"
                : state.planState?.mode === "plan"
                  ? "Plan 模式：描述要规划的任务…"
                  : "发送消息…"
          }
          placeholderColor={theme.textMuted}
          textColor={theme.text}
          focusedTextColor={theme.text}
          cursorColor={theme.primary}
          keyBindings={textareaKeyBindings}
          onSizeChange={function () {
            setTextareaWidth(Math.max(1, this.width));
          }}
          onContentChange={() => {
            const v = inputRef?.plainText ?? "";
            setValue(v);
            // 程序化改值（翻历史/粘贴）不算用户键入，不重置历史索引
            if (programmatic) return;
            setHistoryIdx(-1);
            setPickerIndex(0);
          }}
          onPaste={handlePaste}
          onSubmit={handleSubmit}
          ref={(r: TextareaRenderable) => (inputRef = r)}
        />
      </box>
    </box>
  );
}
