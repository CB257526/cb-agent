/**
 * cb-agent OTUI 主壳。
 *
 * Provider 栈（由外到内）：Theme → Transport → Session。
 * 布局（M2）：纵向 [消息列表 (flexGrow) | Prompt 输入]。
 * M6 会在右侧加 Sidebar、底部加 Footer/StatusBar，扩成三栏。
 *
 * Ctrl-C 行为：busy 时取消当前轮，空闲时退出（quit + 销毁渲染器）。
 */

import { useKeyboard, useRenderer, useTerminalDimensions } from "@opentui/solid";
import type { KeyEvent } from "@opentui/core";
import { Show } from "solid-js";
import type { Transport } from "./transport.js";
import { theme } from "./theme.js";
import { ThemeProvider } from "./context/theme.js";
import { TransportProvider } from "./context/transport.js";
import { SessionProvider, useSession } from "./context/session.js";
import { MessageList } from "./components/MessageList.js";
import { Prompt } from "./components/Prompt.js";
import { Sidebar } from "./components/Sidebar.js";
import { Footer } from "./components/Footer.js";
import { ActivityPanel } from "./components/ActivityPanel.js";
import { SelectDialog } from "./components/SelectDialog.js";

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
  const { state, toggleActivity, setItems, closeDialog } = useSession();

  useKeyboard((key: KeyEvent) => {
    if (key.ctrl && key.name === "c") {
      const selected = renderer.getSelection()?.getSelectedText?.() ?? "";
      if (selected) {
        key.preventDefault?.();
        renderer.copyToClipboardOSC52(selected);
        renderer.clearSelection();
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
      // 仿 bash Ctrl-L：清当前可视对话流，但保留后端 history（与 /clear 区别）。
      // scrollbox 是独立屏幕缓冲，清空 items 即得到干净屏，无需操作终端 scrollback。
      setItems([]);
    }
  });

  return (
    <box
      width={dimensions().width}
      height={dimensions().height}
      flexDirection="column"
      backgroundColor={theme.background}
    >
      {/* 上半部分：左主区（消息+输入）+ 右 Sidebar */}
      <box flexDirection="row" flexGrow={1} minHeight={0}>
        <box flexDirection="column" flexGrow={1} minWidth={0} paddingLeft={1} paddingRight={1}>
          <MessageList />
          <ActivityPanel logFile={props.transport.stderrLogFile} />
          <Prompt />
        </box>
        <Sidebar />
      </box>
      {/* 底部状态栏 */}
      <Footer />
      {/* 浮层 Select 弹窗（/sessions /tools /mcp 等），覆盖在最上层 */}
      <Show when={state.dialog}>
        <SelectDialog spec={state.dialog!} />
      </Show>
    </box>
  );
}
