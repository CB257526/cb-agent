#!/usr/bin/env bun
/**
 * cb-agent OpenTUI 入口。
 *
 * 启动顺序：
 *   1. 解析 python 路径（CB_AGENT_PYTHON → ../venv → 兜底 system python）
 *   2. new Transport：spawn `python run_agent.py --transport jsonrpc --memory-system light`
 *   3. createCliRenderer（exitOnCtrlC=false，App 自己接管 Ctrl-C）
 *   4. render(App)，把 transport 注入
 *
 * python 路径解析逻辑沿用旧 ui-tui/entry.tsx。
 */

import { render } from "@opentui/solid";
import { createCliRenderer } from "@opentui/core";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { App } from "./app.js";
import { Transport } from "./transport.js";
import { win32DisableProcessedInput, win32InstallCtrlCGuard } from "./terminal-win32.js";
import { appendOtuiDiagnostic, installProcessDiagnostics } from "./diagnostics.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function resolvePython(projectRoot: string): string {
  if (process.env.CB_AGENT_PYTHON) return process.env.CB_AGENT_PYTHON;

  const candidates = [
    join(projectRoot, "..", "venv", "python.exe"),
    join(projectRoot, "..", "venv", "Scripts", "python.exe"),
    join(projectRoot, "..", "venv", "bin", "python"),
    join(projectRoot, "venv", "python.exe"),
    join(projectRoot, "venv", "Scripts", "python.exe"),
    join(projectRoot, "venv", "bin", "python"),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return process.platform === "win32" ? "python" : "python3";
}

async function main() {
  // ui-otui/src/entry.tsx → ui-otui/src → ui-otui → cb-agent
  const projectRoot = resolve(__dirname, "..", "..");
  const workspaceCwd = resolve(process.env.CBAGENT_WORKSPACE || projectRoot);
  installProcessDiagnostics(workspaceCwd);
  const python = resolvePython(projectRoot);

  const transport = new Transport({
    python,
    agentRoot: projectRoot,
    workspaceCwd,
  });

  const renderer = await createCliRenderer({
    exitOnCtrlC: false,
    targetFps: 60,
    useKittyKeyboard: {},
    useMouse: true, // 必须开，否则 <scrollbox> 收不到滚轮事件（M3 验收核心）
  });

  // Windows：清掉 stdin 的 ENABLE_PROCESSED_INPUT，否则 Ctrl-C 会被控制台
  // 直接转成 SIGINT 杀进程，exitOnCtrlC:false 拦不住。装一个 guard 持续保证
  // 该标志位关闭（setRawMode 切换 / 外部改回都会被重新清掉）。
  win32DisableProcessedInput();
  const unhookCtrlCGuard = win32InstallCtrlCGuard();
  renderer.once("destroy", () => {
    appendOtuiDiagnostic("renderer destroyed");
    unhookCtrlCGuard?.();
  });

  render(() => <App transport={transport} />, renderer);
}

main();
