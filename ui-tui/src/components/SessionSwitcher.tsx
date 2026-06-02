import React, { useEffect, useState } from "react";
import { Box, Text, useInput } from "ink";
import { theme } from "../theme.js";
import type { SessionSummary } from "../types.js";

interface Props {
  sessions: SessionSummary[];
  currentSessionId?: string | null;
  loading: boolean;
  error?: string | null;
  onSwitch: (sessionId: string) => void;
  onNew: () => void;
  onRefresh: () => void;
  onCancel: () => void;
}

/**
 * 本地会话切换面板。
 *
 * 这个组件只展示 LocalSessionStore 返回的轻量摘要：session_id、turn_count、
 * active_task/rolling_summary preview 等。它不会读取 transcript 全文，也不会把
 * 压缩 trace 展开到 UI；真正切换时由 App 调 session.switch，再用后端返回的
 * 普通 history 重绘当前屏幕。
 */
export function SessionSwitcher({
  sessions,
  currentSessionId,
  loading,
  error,
  onSwitch,
  onNew,
  onRefresh,
  onCancel,
}: Props) {
  const [selected, setSelected] = useState(0);

  // 列表刷新后把选中位置夹到合法范围，避免删除/新建后 selected 指向空项。
  useEffect(() => {
    setSelected((idx) => Math.min(Math.max(idx, 0), Math.max(sessions.length - 1, 0)));
  }, [sessions.length]);

  useInput((input, key) => {
    if (key.escape) {
      onCancel();
      return;
    }
    if (key.upArrow) {
      setSelected((idx) => Math.max(0, idx - 1));
      return;
    }
    if (key.downArrow) {
      setSelected((idx) => Math.min(Math.max(sessions.length - 1, 0), idx + 1));
      return;
    }
    if (key.return) {
      const item = sessions[selected];
      if (item?.session_id) onSwitch(item.session_id);
      return;
    }
    const ch = input.toLowerCase();
    if (ch === "n") {
      onNew();
      return;
    }
    if (ch === "r") {
      onRefresh();
    }
  });

  return (
    <Box borderStyle="round" borderColor={theme.suggestion} paddingX={1} flexDirection="column">
      <Box justifyContent="space-between">
        <Text color={theme.suggestion} bold>本地会话</Text>
        <Text dimColor>Enter 切换 · n 新建 · r 刷新 · Esc 关闭</Text>
      </Box>

      {loading ? <Text dimColor>正在读取 .cbagent/sessions...</Text> : null}
      {error ? <Text color={theme.error}>{error}</Text> : null}

      {!loading && !sessions.length ? (
        <Text dimColor>还没有可切换的会话。按 n 新建空白会话。</Text>
      ) : null}

      {sessions.map((s, idx) => {
        const active = s.session_id === currentSessionId || !!s.is_active;
        const highlighted = idx === selected;
        const preview = s.active_task || s.rolling_summary || "空会话";
        return (
          <Box key={s.session_id} flexDirection="column" marginTop={idx === 0 ? 1 : 0}>
            <Box>
              <Text color={highlighted ? theme.suggestion : undefined} bold={highlighted}>
                {highlighted ? "> " : "  "}
                {active ? "* " : "  "}
                {s.session_id}
              </Text>
              <Text dimColor>
                {"  "}
                turns={s.turn_count ?? 0}
                {"  "}
                updated={formatTime(s.updated_at)}
              </Text>
            </Box>
            <Box paddingLeft={highlighted ? 2 : 4}>
              <Text dimColor>{preview.slice(0, 120)}</Text>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}

function formatTime(value?: string): string {
  if (!value) return "?";
  // ISO 字符串直接截到秒，终端列表里比本地化长日期更稳定、也更省宽度。
  return value.replace("T", " ").slice(0, 19);
}
