/**
 * AttachmentQueue：待发送附件队列（OTUI 版）。
 *
 * 使用 OpenTUI 的 box/text 渲染。队列非空时显示在 Prompt 上方，
 * 提示用户下一条消息会带哪些文件。发送成功后由 session 的 onResponse 清空。
 */

import { For, Show } from "solid-js";
import { useTheme } from "../context/theme.js";
import { useSession } from "../context/session.js";
import type { QueuedAttachment } from "../types.js";

function formatBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return `${value}B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)}KB`;
  return `${(value / 1024 / 1024).toFixed(1)}MB`;
}

const IMAGE_EXT = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif"];
const AUDIO_EXT = ["mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"];

function inferKind(item: QueuedAttachment): string {
  if (item.modality) return item.modality;
  const ext = item.fileName.split(".").pop()?.toLowerCase() ?? "";
  if (IMAGE_EXT.includes(ext)) return "image";
  if (AUDIO_EXT.includes(ext)) return "audio";
  return "file";
}

export function AttachmentQueue() {
  const theme = useTheme();
  const { state } = useSession();

  return (
    <Show when={state.attachments.length > 0}>
      <box flexDirection="column" flexShrink={0} marginBottom={1}>
        <text fg={theme.info}>附件队列（发送后清空）</text>
        <For each={state.attachments}>
          {(item, index) => {
            const size = formatBytes(item.size);
            const suffix = [inferKind(item), item.source ?? "direct", size].filter(Boolean).join(" · ");
            return (
              <text fg={theme.text}>
                <span style={{ fg: theme.suggestion }}>{`${index() + 1}. `}</span>
                {item.fileName}
                <span style={{ fg: theme.textMuted }}>{suffix ? `  ${suffix}` : ""}</span>
              </text>
            );
          }}
        </For>
      </box>
    </Show>
  );
}
