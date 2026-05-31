import React from "react";
import { Text } from "ink";

interface Props {
  /** 例: "Ctrl-O", "Enter", "↑/↓" */
  shortcut: string;
  /** 例: "expand", "send", "navigate history" */
  action: string;
  /** 包成 (xxx to yyy) */
  parens?: boolean;
  /** 把 shortcut 加粗 */
  bold?: boolean;
}

/** 单个快捷键提示，常和 Byline 配合："Enter to send · ↑/↓ history · Ctrl-O log" */
export function KeyboardShortcutHint({ shortcut, action, parens = false, bold = false }: Props) {
  const sc = bold ? <Text bold>{shortcut}</Text> : shortcut;
  if (parens) return <Text>({sc} to {action})</Text>;
  return <Text>{sc} to {action}</Text>;
}
