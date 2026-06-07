/**
 * Stage 5b transport smoke test：不挂 Ink，只测 transport 跟 Python gateway 能正常握手。
 *
 * 跑法（在 ui-tui/ 目录）：
 *   npx tsx src/__smoke__/transport.smoke.ts
 *
 * 期望输出：
 *   [transport] event gateway_ready model=...
 *   [transport] sending prompt
 *   [transport] event text_delta ...
 *   [transport] event done ...
 *   [transport] exit code=0
 */

import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { Transport } from "../transport.js";

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
  for (const p of candidates) if (existsSync(p)) return p;
  // Linux 服务器经常没有 python 命令，只提供 python3；这里和正式 TUI 入口保持一致。
  return process.platform === "win32" ? "python" : "python3";
}

const projectRoot = resolve(__dirname, "..", "..", "..");
const python = resolvePython(projectRoot);

console.log(`[smoke] python=${python}`);
console.log(`[smoke] cwd=${projectRoot}`);

const t = new Transport({ python, cwd: projectRoot });

let ready = false;
let doneSeen = false;

t.on("event", (ev: any) => {
  console.log(`[transport] event ${ev.type}` + (ev.delta ? ` delta=${JSON.stringify(ev.delta).slice(0, 60)}` : "")
    + (ev.model ? ` model=${ev.model}` : "")
    + (ev.name ? ` name=${ev.name}` : ""));
  if (ev.type === "gateway_ready") {
    ready = true;
    console.log("[transport] sending session.quit (smoke 只测握手，不打 LLM)");
    t.quit();
  }
  if (ev.type === "done") doneSeen = true;
});

t.on("response", (id, body) => {
  console.log(`[transport] response id=${id} ${JSON.stringify(body)}`);
});

t.on("protocolError", (raw, err) => {
  console.error(`[transport] protocolError: ${err.message} raw=${raw.slice(0, 100)}`);
});

t.on("exit", (code) => {
  console.log(`[transport] exit code=${code}`);
  console.log(`[smoke] result: ready=${ready} doneSeen=${doneSeen}`);
  process.exit(ready && code === 0 ? 0 : 1);
});

setTimeout(() => {
  console.error("[smoke] timeout, killing");
  t.close();
  process.exit(2);
}, 30000);
