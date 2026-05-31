import React from "react";
import { Box, Text } from "ink";

export interface ActivityPanelProps {
  lines: string[];
  /** 是否可见（折叠时占位极小，不渲染内容） */
  visible: boolean;
  /** 显示最近 N 行（lines 数组本身已经是 ring buffer 截断过的，这里再保险截一次） */
  maxLines?: number;
  /** 日志文件路径，提示用户去看完整内容 */
  logFile?: string;
}

/**
 * 后端 stderr 实时面板。Hermes 同款，调试时不用切窗口 tail 日志。
 *
 * 不可见时只渲染一行折叠提示，避免占用对话流空间。
 */
export function ActivityPanel({ lines, visible, maxLines = 12, logFile }: ActivityPanelProps) {
  if (!visible) {
    return (
      <Box>
        <Text color="gray">ⓘ Ctrl-O 显示后端日志（{lines.length} 行可用）</Text>
      </Box>
    );
  }

  const tail = lines.slice(-maxLines);
  return (
    <Box flexDirection="column" borderStyle="round" borderColor="gray" paddingX={1}>
      <Box justifyContent="space-between">
        <Text color="cyan" bold>后端日志（最近 {tail.length}/{lines.length} 行）</Text>
        <Text color="gray">Ctrl-O 隐藏</Text>
      </Box>
      {tail.length === 0 ? (
        <Text color="gray">（暂无）</Text>
      ) : (
        tail.map((line, i) => (
          <Text key={i} color={colorize(line)} wrap="truncate-end">{line || " "}</Text>
        ))
      )}
      {logFile && (
        <Box marginTop={1}>
          <Text color="gray" wrap="truncate-middle">完整日志：{logFile}</Text>
        </Box>
      )}
    </Box>
  );
}

function colorize(line: string): string | undefined {
  // 简单按内容上色：错误/警告醒目一点，启动期信息灰一点
  if (/error|exception|traceback|✗/i.test(line)) return "red";
  if (/warn/i.test(line)) return "yellow";
  if (/^\[.*\]/.test(line)) return "gray";  // 形如 [info] 的 section 标记
  return undefined;
}
