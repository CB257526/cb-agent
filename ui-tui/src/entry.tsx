#!/usr/bin/env node
/**
 * cb-agent TUI 入口。
 *
 * 启动顺序：
 *   1. spawn `python run_agent.py --transport jsonrpc`（cwd=cb-agent 项目根）
 *   2. 用 ink.render 挂载 App，把 transport 实例传进去
 *   3. exitOnCtrlC=false：自己处理 Ctrl-C（busy 时 cancel，空闲时 quit + exit）
 *
 * Python 路径解析优先级：
 *   1. CB_AGENT_PYTHON 环境变量
 *   2. ../venv/python.exe（Windows）或 ../venv/bin/python（POSIX）
 *   3. POSIX 上兜底 system "python3"，Windows 上兜底 system "python"
 *
 * 故障 hint：
 *   - Python 端启动失败 → exit code 非 0 → App 显示 "进程退出"，stderr 全在
 *     .cbagent/logs/system/gateway-<ts>.log，让用户去看
 *   - 协议解析失败 → 通常是 Python 端漏了 stdout 重定向（比如调试时加了 print 没走 _info）
 *     UI 显示一行警告 + 日志路径
 */

import React from "react";
import { render } from "ink";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { App } from "./App.js";
import { Transport } from "./transport.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function resolvePython(projectRoot: string): string {
  if (process.env.CB_AGENT_PYTHON) return process.env.CB_AGENT_PYTHON;

  const candidates = [
    join(projectRoot, "..", "venv", "python.exe"),                  // Windows
    join(projectRoot, "..", "venv", "Scripts", "python.exe"),       // Windows alt
    join(projectRoot, "..", "venv", "bin", "python"),               // POSIX
    join(projectRoot, "venv", "python.exe"),
    join(projectRoot, "venv", "Scripts", "python.exe"),
    join(projectRoot, "venv", "bin", "python"),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  // 许多 Linux 发行版默认只提供 python3，不提供 python；Windows 则通常通过 python 启动器或安装目录暴露 python。
  return process.platform === "win32" ? "python" : "python3";
}

function main() {
  // ui-tui/dist/entry.js → ui-tui/ → cb-agent/
  // ui-tui/src/entry.tsx (tsx 模式) → ui-tui/src/ → ui-tui/ → cb-agent/
  const projectRoot = resolve(__dirname, __dirname.endsWith("src") ? ".." : ".", "..");
  const python = resolvePython(projectRoot);

  const transport = new Transport({
    python,
    cwd: projectRoot,
  });

  // 把 ink instance 的 clear() 透传给 App，给 Ctrl+L 用
  // 用 closure 解决"render 时 instance 还没生成"的鸡蛋问题
  let inkInstance: ReturnType<typeof render> | null = null;
  const clearScreen = () => inkInstance?.clear();

  inkInstance = render(<App transport={transport} clearScreen={clearScreen} />, {
    exitOnCtrlC: false,  // App 自己处理 Ctrl-C
  });

  inkInstance.waitUntilExit().finally(() => {
    transport.close();
  });
}

main();
