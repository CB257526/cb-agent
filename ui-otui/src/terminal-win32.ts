/**
 * Windows 控制台输入模式修正（Ctrl-C 根治点）。
 *
 * 问题：Windows 控制台 stdin 默认开着 ENABLE_PROCESSED_INPUT。这个标志位会让
 * 终端在收到 Ctrl-C 时直接产生一个 CTRL_C_EVENT（等价 SIGINT）把进程杀掉，
 * 而不是把字节交给 stdin 让程序自己处理。结果就是 createCliRenderer 的
 * exitOnCtrlC:false 完全拦不住——按 Ctrl-C 进程直接退出，App 里的 useKeyboard
 * 根本收不到这个键。
 *
 * 解决：用 bun:ffi 调 kernel32 的 SetConsoleMode 清掉 ENABLE_PROCESSED_INPUT。
 * 之后 Ctrl-C 会作为普通 \x03 字节进入 stdin，由 OpenTUI 解析成 KeyEvent，
 * App 的 useKeyboard 就能接管（busy 时 cancel，空闲时 quit）。
 *
 * 难点：这个标志位是“控制台全局”的，且某些运行时会在后续 tick 重新打开它
 * （尤其 setRawMode 切换时）。所以单次清除不够，需要：
 *   1. 包裹 stdin.setRawMode，在每次切换后重新清；
 *   2. 低频轮询兜底，应对原生/外部改回模式。
 *
 * 实现整体移植自 opencode packages/tui/src/terminal-win32.ts。
 */

import { dlopen, ptr } from "bun:ffi";
import type { ReadStream } from "node:tty";

const STD_INPUT_HANDLE = -10;
const ENABLE_PROCESSED_INPUT = 0x0001;

const kernel = () =>
  dlopen("kernel32.dll", {
    GetStdHandle: { args: ["i32"], returns: "ptr" },
    GetConsoleMode: { args: ["ptr", "ptr"], returns: "i32" },
    SetConsoleMode: { args: ["ptr", "u32"], returns: "i32" },
    FlushConsoleInputBuffer: { args: ["ptr"], returns: "i32" },
  });

let k32: ReturnType<typeof kernel> | undefined;

function load(): boolean {
  if (process.platform !== "win32") return false;
  try {
    k32 ??= kernel();
    return true;
  } catch {
    return false;
  }
}

/** 立即清除 stdin 上的 ENABLE_PROCESSED_INPUT（启动时调一次）。 */
export function win32DisableProcessedInput(): void {
  if (process.platform !== "win32") return;
  if (!process.stdin.isTTY) return;
  if (!load()) return;

  const handle = k32!.symbols.GetStdHandle(STD_INPUT_HANDLE);
  const buf = new Uint32Array(1);
  if (k32!.symbols.GetConsoleMode(handle, ptr(buf)) === 0) return;

  const mode = buf[0]!;
  if ((mode & ENABLE_PROCESSED_INPUT) === 0) return;
  k32!.symbols.SetConsoleMode(handle, mode & ~ENABLE_PROCESSED_INPUT);
}

let unhook: (() => void) | undefined;

/**
 * 持续保证 ENABLE_PROCESSED_INPUT 处于关闭状态。
 *
 * 组合手段：
 *   - 包裹 setRawMode：已知的 raw-mode 切换后重新清除；
 *   - 低频轮询：应对原生/外部模式变更的兜底。
 *
 * 返回一个 unhook 函数（退出时调用还原原始模式）。
 */
export function win32InstallCtrlCGuard(): (() => void) | undefined {
  if (process.platform !== "win32") return;
  if (!process.stdin.isTTY) return;
  if (!load()) return;
  if (unhook) return unhook;

  const stdin = process.stdin as ReadStream;
  const original = stdin.setRawMode;

  const handle = k32!.symbols.GetStdHandle(STD_INPUT_HANDLE);
  const buf = new Uint32Array(1);

  if (k32!.symbols.GetConsoleMode(handle, ptr(buf)) === 0) return;
  const initial = buf[0]!;

  const enforce = () => {
    if (k32!.symbols.GetConsoleMode(handle, ptr(buf)) === 0) return;
    const mode = buf[0]!;
    if ((mode & ENABLE_PROCESSED_INPUT) === 0) return;
    k32!.symbols.SetConsoleMode(handle, mode & ~ENABLE_PROCESSED_INPUT);
  };

  // 某些运行时会在下一个 tick 重新打开模式；连续 enforce 两次。
  const later = () => {
    enforce();
    setImmediate(enforce);
  };

  let wrapped: ReadStream["setRawMode"] | undefined;

  if (typeof original === "function") {
    wrapped = (mode: boolean) => {
      const result = original.call(stdin, mode);
      later();
      return result;
    };
    stdin.setRawMode = wrapped;
  }

  // 立即清一次（覆盖更早的模式变更）。
  later();

  const interval = setInterval(enforce, 100);
  interval.unref();

  let done = false;
  unhook = () => {
    if (done) return;
    done = true;
    clearInterval(interval);
    if (wrapped && stdin.setRawMode === wrapped) {
      stdin.setRawMode = original;
    }
    k32!.symbols.SetConsoleMode(handle, initial);
    unhook = undefined;
  };

  return unhook;
}
