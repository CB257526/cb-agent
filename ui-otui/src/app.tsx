/**
 * cb-agent OTUI 主壳。
 *
 * Provider 栈（由外到内）：Theme → Transport → Session。
 * 布局：单列 [会话标题 | 消息列表 | 活动问询 | Prompt | Footer]。
 *
 * Ctrl-C 行为：busy 时取消当前轮，空闲时退出（quit + 销毁渲染器）。
 */

import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/solid";
import type { KeyEvent } from "@opentui/core";
import { createMemo, onCleanup, Show } from "solid-js";
import type { Transport } from "./transport.js";
import { theme } from "./theme.js";
import { ThemeProvider } from "./context/theme.js";
import { TransportProvider } from "./context/transport.js";
import { SessionProvider, useSession } from "./context/session.js";
import { MessageList } from "./components/MessageList.js";
import { Prompt } from "./components/Prompt.js";
import { Footer } from "./components/Footer.js";
import { ActivityPanel } from "./components/ActivityPanel.js";
import { SelectDialog } from "./components/SelectDialog.js";
import { QuestionPanel } from "./components/QuestionPanel.js";
import { SessionHeader } from "./components/SessionHeader.js";
import { writeClipboardText } from "./clipboardImage.js";
import { getHorizontalPadding } from "./layout.js";
import { applySelectionColors } from "./selection.js";

export function App(props: { transport: Transport }) {
  return (
    <ThemeProvider>
      <TransportProvider transport={props.transport}>
        <SessionProvider>
          <Shell transport={props.transport} />
        </SessionProvider>
      </TransportProvider>
    </ThemeProvider>
  );
}

function Shell(props: { transport: Transport }) {
  const dimensions = useTerminalDimensions();
  const renderer = useRenderer();
  const { state, toggleActivity, clearViewport, closeDialog, appendSystem } = useSession();
  const horizontalPadding = () => getHorizontalPadding(dimensions().width);
  const activeQuestion = createMemo(() =>
    state.items.find(
      (item) =>
        item.role === "ask_question"
        && !item.answered
        && item.questionId === state.activeQuestionId,
    ),
  );

  // OpenTUI 的 Markdown 节点会在渲染过程中动态重建；在每帧完成后遍历一次渲染树，
  // 保证新旧消息、代码块和输入框都能用同一套可见选区，不依赖组件逐个传样式。
  const onFrame = () => applySelectionColors(renderer.root);
  renderer.on("frame", onFrame);
  onCleanup(() => {
    renderer.off("frame", onFrame);
  });

  const copySelectionText = (text: string): boolean => {
    if (!text) return false;
    let osc52Copied = false;
    try {
      osc52Copied = renderer.copyToClipboardOSC52(text);
    } catch {
      osc52Copied = false;
    }
    writeClipboardText(text).catch((error) => {
      const fallback = osc52Copied ? "（已尝试 OSC52 终端复制）" : "";
      appendSystem(`复制选区失败：${(error as Error).message}${fallback}`);
    });
    renderer.clearSelection();
    return true;
  };

  const previousCopySelection = renderer.console.onCopySelection;
  renderer.console.onCopySelection = (text: string) => {
    copySelectionText(text);
  };
  onCleanup(() => {
    renderer.console.onCopySelection = previousCopySelection;
  });

  useKeyboard((key: KeyEvent) => {
    if (key.ctrl && key.name === "c") {
      const selected = renderer.getSelection()?.getSelectedText?.() ?? "";
      if (selected) {
        key.preventDefault?.();
        key.stopPropagation?.();
        copySelectionText(selected);
        return;
      }
    }

    // 弹窗打开时：Ctrl-C 只关弹窗，不退出（其余键交给 SelectDialog 自身处理）
    if (state.dialog) {
      if (key.ctrl && key.name === "c") {
        key.preventDefault?.();
        closeDialog();
      }
      return;
    }
    if (key.ctrl && key.name === "c") {
      if (state.busy) {
        props.transport.cancel();
      } else {
        props.transport.quit();
        setTimeout(() => {
          renderer.destroy();
          process.exit(0);
        }, 200);
      }
    } else if (key.ctrl && key.name === "o") {
      toggleActivity();
    } else if (key.ctrl && key.name === "l") {
      // 仿 bash Ctrl-L：只清主视口，不删对话 items / 后端 history。
      // 在流末尾插入与可视区等高的空白占位并滚到底；上滑仍可看到之前消息。
      // 真正清空会话请用 /clear。
      key.preventDefault?.();
      // 兜底高度：终端总高减去标题/输入/Footer 等 chrome（约 6 行）。
      const fallbackHeight = Math.max(4, dimensions().height - 6);
      clearViewport({ height: fallbackHeight });
    }
  });

  return (
    <box
      width={dimensions().width}
      height={dimensions().height}
      flexDirection="column"
      backgroundColor={theme.background}
      paddingLeft={horizontalPadding()}
      paddingRight={horizontalPadding()}
    >
      {/* 单列主壳让消息流在常见的 80 列终端中仍有足够阅读宽度。 */}
      <SessionHeader />
      <MessageList />
      <ActivityPanel logFile={props.transport.stderrLogFile} />
      {/* 未回答的问题固定在输入区上方，回答后才回到历史流中。 */}
      <Show when={activeQuestion()}>
        {(item) => <QuestionPanel item={item()} />}
      </Show>
      {/* 编辑器拥有独立缓冲；交互浮层出现时卸载它，避免旧光标覆盖新的底部面板。 */}
      <Show when={!activeQuestion() && !state.dialog}>
        <Prompt />
      </Show>
      <Footer />
      {/* 浮层 Select 弹窗（/sessions /tools /mcp 等），覆盖在最上层 */}
      <Show when={state.dialog}>
        <SelectDialog spec={state.dialog!} />
      </Show>
    </box>
  );
}
