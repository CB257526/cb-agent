import { execFile, spawn } from "node:child_process";
import { createWriteStream, mkdirSync, statSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { promisify } from "node:util";
import type { QueuedAttachment } from "./types.js";

const execFileAsync = promisify(execFile);
const NO_IMAGE_EXIT = 2;
const NO_TEXT_EXIT = 3;
const NO_FILES_EXIT = 4;

export type ClipboardPaste =
  | { kind: "text"; text: string }
  | { kind: "files"; paths: string[] }
  | { kind: "image"; attachment: QueuedAttachment };

let seq = 0;

function attachmentPath(): string {
  const dir = join(homedir(), ".cb-agent", "attachments");
  mkdirSync(dir, { recursive: true });
  seq += 1;
  return join(dir, `clipboard-${Date.now()}-${seq}.png`);
}

function queuedFromPath(path: string): QueuedAttachment {
  let size: number | null = null;
  try {
    size = statSync(path).size;
  } catch {
    size = null;
  }
  return {
    id: `clip_${Date.now()}_${seq}`,
    path,
    source: "clipboard",
    modality: "image",
    fileName: basename(path),
    size,
  };
}

function cleanup(path: string): void {
  try {
    unlinkSync(path);
  } catch {
    // Ignore missing or locked temp files.
  }
}

function psBase64(value: string): string {
  return Buffer.from(value, "utf8").toString("base64");
}

function errorCode(error: unknown): number | null {
  const code = (error as { code?: unknown } | null)?.code;
  if (typeof code === "number") return code;
  if (typeof code === "string" && /^\d+$/.test(code)) return Number(code);
  return null;
}

function cleanExecMessage(error: unknown): string {
  const stderr = (error as { stderr?: unknown } | null)?.stderr;
  if (typeof stderr === "string" && stderr.trim()) return stderr.trim();
  const message = (error as Error | null)?.message;
  return message || "未知错误";
}

async function readClipboardTextWindows(): Promise<string | null> {
  const script = `
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::ASCII
Add-Type -AssemblyName System.Windows.Forms
if (-not [System.Windows.Forms.Clipboard]::ContainsText()) { exit ${NO_TEXT_EXIT} }
$text = [System.Windows.Forms.Clipboard]::GetText([System.Windows.Forms.TextDataFormat]::UnicodeText)
$bytes = [System.Text.Encoding]::UTF8.GetBytes($text)
[Console]::Out.Write([Convert]::ToBase64String($bytes))
`;
  try {
    const { stdout } = await execFileAsync("powershell.exe", [
      "-NoProfile",
      "-STA",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      script,
    ], { windowsHide: true, timeout: 4000 });
    return Buffer.from(stdout.trim(), "base64").toString("utf8");
  } catch (error) {
    if (errorCode(error) === NO_TEXT_EXIT) return null;
    throw new Error(`读取剪贴板文本失败：${cleanExecMessage(error)}`);
  }
}

async function readClipboardFilesWindows(): Promise<string[] | null> {
  const script = `
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::ASCII
Add-Type -AssemblyName System.Windows.Forms
if (-not [System.Windows.Forms.Clipboard]::ContainsFileDropList()) { exit ${NO_FILES_EXIT} }
$files = [System.Windows.Forms.Clipboard]::GetFileDropList()
$json = ConvertTo-Json -Compress -InputObject ([string[]]$files)
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
[Console]::Out.Write([Convert]::ToBase64String($bytes))
`;
  try {
    const { stdout } = await execFileAsync("powershell.exe", [
      "-NoProfile",
      "-STA",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      script,
    ], { windowsHide: true, timeout: 4000 });
    const json = Buffer.from(stdout.trim(), "base64").toString("utf8");
    const files = JSON.parse(json);
    return Array.isArray(files) ? files.filter((p) => typeof p === "string" && p.trim()) : null;
  } catch (error) {
    if (errorCode(error) === NO_FILES_EXIT) return null;
    throw new Error(`读取剪贴板文件失败：${cleanExecMessage(error)}`);
  }
}

async function readClipboardImageWindows(target: string): Promise<void> {
  const script = `
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not [System.Windows.Forms.Clipboard]::ContainsImage()) { exit ${NO_IMAGE_EXIT} }
$path = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${psBase64(target)}"))
$img = [System.Windows.Forms.Clipboard]::GetImage()
$img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
$img.Dispose()
`;
  try {
    await execFileAsync("powershell.exe", [
      "-NoProfile",
      "-STA",
      "-ExecutionPolicy",
      "Bypass",
      "-Command",
      script,
    ], { windowsHide: true, timeout: 8000 });
  } catch (error) {
    if (errorCode(error) === NO_IMAGE_EXIT) {
      throw new Error("剪贴板里没有图片。请先复制一张图片，或使用 /attach <path>。");
    }
    throw new Error(`读取剪贴板图片失败：${cleanExecMessage(error)}`);
  }
}

async function commandExists(command: string): Promise<boolean> {
  try {
    await execFileAsync("sh", ["-lc", `command -v ${command} >/dev/null 2>&1`], { timeout: 2000 });
    return true;
  } catch {
    return false;
  }
}

async function readClipboardTextMac(): Promise<string | null> {
  try {
    const { stdout } = await execFileAsync("pbpaste", [], { timeout: 4000 });
    return stdout.length > 0 ? stdout : null;
  } catch {
    return null;
  }
}

async function readClipboardImageMac(target: string): Promise<void> {
  if (!(await commandExists("pngpaste"))) {
    throw new Error("macOS 剪贴板图片需要安装 pngpaste，或改用 /attach <path>。");
  }
  await execFileAsync("pngpaste", [target], { timeout: 8000 });
}

async function readClipboardTextLinux(): Promise<string | null> {
  if (await commandExists("wl-paste")) {
    try {
      const { stdout } = await execFileAsync("wl-paste", ["--no-newline"], { timeout: 4000 });
      return stdout.length > 0 ? stdout : null;
    } catch {
      return null;
    }
  }
  if (await commandExists("xclip")) {
    try {
      const { stdout } = await execFileAsync("xclip", ["-selection", "clipboard", "-o"], { timeout: 4000 });
      return stdout.length > 0 ? stdout : null;
    } catch {
      return null;
    }
  }
  return null;
}

async function readClipboardImageLinux(target: string): Promise<void> {
  if (await commandExists("wl-paste")) {
    await pipeCommandToFile("wl-paste", ["--type", "image/png"], target);
    return;
  }
  if (await commandExists("xclip")) {
    await pipeCommandToFile("xclip", ["-selection", "clipboard", "-t", "image/png", "-o"], target);
    return;
  }
  throw new Error("Linux 剪贴板图片需要 wl-paste 或 xclip，或改用 /attach <path>。");
}

function pipeCommandToFile(command: string, args: string[], target: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"] });
    const out = createWriteStream(target);
    let childCode: number | null = null;
    let outFinished = false;
    let settled = false;
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      finish(new Error(`${command} 读取剪贴板超时`));
    }, 8000);

    const finish = (err?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (err) reject(err);
      else resolve();
    };

    let stderr = "";
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
    });
    child.stdout.pipe(out);
    child.on("error", finish);
    child.on("close", (code) => {
      childCode = code;
      if (code !== 0) finish(new Error(stderr.trim() || `${command} 退出码 ${code}`));
      else if (outFinished) finish();
    });
    out.on("error", finish);
    out.on("finish", () => {
      outFinished = true;
      if (childCode === 0) finish();
    });
  });
}

export async function readClipboardText(): Promise<string | null> {
  if (process.platform === "win32") return readClipboardTextWindows();
  if (process.platform === "darwin") return readClipboardTextMac();
  return readClipboardTextLinux();
}

export async function readClipboardFiles(): Promise<string[] | null> {
  if (process.platform === "win32") return readClipboardFilesWindows();
  return null;
}

export async function readClipboardImageAttachment(): Promise<QueuedAttachment> {
  const target = attachmentPath();
  try {
    if (process.platform === "win32") {
      await readClipboardImageWindows(target);
    } else if (process.platform === "darwin") {
      await readClipboardImageMac(target);
    } else {
      await readClipboardImageLinux(target);
    }
    return queuedFromPath(target);
  } catch (error) {
    cleanup(target);
    throw error;
  }
}

export async function readClipboardForPaste(): Promise<ClipboardPaste> {
  const files = await readClipboardFiles();
  if (files && files.length > 0) {
    return { kind: "files", paths: files };
  }

  const text = await readClipboardText();
  if (text !== null && text.length > 0) {
    return { kind: "text", text };
  }

  try {
    const attachment = await readClipboardImageAttachment();
    return { kind: "image", attachment };
  } catch (error) {
    if (text === "") throw new Error("剪贴板文本为空，也没有可粘贴的图片或文件。");
    throw error;
  }
}
