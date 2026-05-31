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
 *   3. system "python"
 *
 * 故障 hint：
 *   - Python 端启动失败 → exit code 非 0 → App 显示 "进程退出"，stderr 全在
 *     ~/.cb-agent/logs/gateway-<ts>.log，让用户去看
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
    join(projectRoot, "venv", "bin", "python"),
  ];
  for (const p of candidates) {
    if (existsSync(p)) return p;
  }
  return "python";
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

  const { waitUntilExit } = render(<App transport={transport} />, {
    exitOnCtrlC: false,  // App 自己处理 Ctrl-C
  });

  waitUntilExit().finally(() => {
    transport.close();
  });
}

main();
