import { execFile, spawn } from "node:child_process";
import { createWriteStream, mkdirSync, statSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { promisify } from "node:util";
import type { QueuedAttachment } from "./types.js";

const execFileAsync = promisify(execFile);

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
    // 失败时说明文件不存在或已被系统占用；这里不阻断用户继续输入。
  }
}

function psBase64(value: string): string {
  return Buffer.from(value, "utf8").toString("base64");
}

async function readClipboardImageWindows(target: string): Promise<void> {
  const script = `
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$path = [System.Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("${psBase64(target)}"))
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($null -eq $img) {
  [Console]::Error.WriteLine("剪贴板里没有图片。请先复制一张图片，或使用 /attach <path>。")
  exit 2
}
$img.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
$img.Dispose()
`;
  await execFileAsync("powershell.exe", [
    "-NoProfile",
    "-STA",
    "-ExecutionPolicy",
    "Bypass",
    "-Command",
    script,
  ], { windowsHide: true, timeout: 8000 });
}

async function commandExists(command: string): Promise<boolean> {
  try {
    await execFileAsync("sh", ["-lc", `command -v ${command} >/dev/null 2>&1`], { timeout: 2000 });
    return true;
  } catch {
    return false;
  }
}

async function readClipboardImageMac(target: string): Promise<void> {
  if (!(await commandExists("pngpaste"))) {
    throw new Error("macOS 剪贴板图片需要安装 pngpaste，或改用 /attach <path>。");
  }
  await execFileAsync("pngpaste", [target], { timeout: 8000 });
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
    child.on("error", (err) => {
      finish(err);
    });
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
