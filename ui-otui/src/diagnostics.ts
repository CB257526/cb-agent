import { appendFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";

let installed = false;
let otuiLogPath = "";

function formatError(error: unknown): string {
  if (error instanceof Error) {
    return error.stack || `${error.name}: ${error.message}`;
  }
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
}

export function appendOtuiDiagnostic(message: string, error?: unknown): void {
  if (!otuiLogPath) return;
  const suffix = error === undefined ? "" : `\n${formatError(error)}`;
  try {
    appendFileSync(
      otuiLogPath,
      `[${new Date().toISOString()}] ${message}${suffix}\n`,
      "utf8",
    );
  } catch {
    // Diagnostics must never become the reason the TUI exits.
  }
}

export function installProcessDiagnostics(workspaceCwd: string): string {
  if (installed) return otuiLogPath;
  installed = true;

  const logsDir = join(workspaceCwd, ".cbagent", "logs", "system");
  mkdirSync(logsDir, { recursive: true });
  otuiLogPath = join(logsDir, `otui-${Date.now()}.log`);
  appendOtuiDiagnostic(`OTUI diagnostics started pid=${process.pid}`);

  process.on("uncaughtException", (error) => {
    appendOtuiDiagnostic("uncaughtException", error);
  });
  process.on("unhandledRejection", (reason) => {
    appendOtuiDiagnostic("unhandledRejection", reason);
  });
  process.on("warning", (warning) => {
    appendOtuiDiagnostic("process warning", warning);
  });
  process.on("exit", (code) => {
    appendOtuiDiagnostic(`process exit code=${code}`);
  });

  return otuiLogPath;
}

export function otuiDiagnosticLogFile(): string {
  return otuiLogPath;
}
