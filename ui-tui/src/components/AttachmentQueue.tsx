import React from "react";
import { Box, Text } from "ink";
import { theme } from "../theme.js";
import type { QueuedAttachment } from "../types.js";

function formatBytes(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return `${value}B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)}KB`;
  return `${(value / 1024 / 1024).toFixed(1)}MB`;
}

function inferKind(item: QueuedAttachment): string {
  if (item.modality) return item.modality;
  const ext = item.fileName.split(".").pop()?.toLowerCase() ?? "";
  if (["png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif"].includes(ext)) return "image";
  if (["mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"].includes(ext)) return "audio";
  return "file";
}

export function AttachmentQueue({ attachments }: { attachments: QueuedAttachment[] }) {
  if (!attachments.length) return null;
  return (
    <Box flexDirection="column" marginBottom={1}>
      <Text color={theme.info}>附件队列（发送后清空）</Text>
      {attachments.map((item, index) => {
        const size = formatBytes(item.size);
        const suffix = [inferKind(item), item.source ?? "direct", size].filter(Boolean).join(" · ");
        return (
          <Text key={item.id}>
            <Text color={theme.suggestion}>{index + 1}. </Text>
            <Text>{item.fileName}</Text>
            <Text dimColor>{suffix ? `  ${suffix}` : ""}</Text>
          </Text>
        );
      })}
    </Box>
  );
}
