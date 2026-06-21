import React, { useMemo } from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { ToolBlock } from "./ToolBlock.js";
import { Pane } from "./Pane.js";
import { AskQuestionPanel } from "./AskQuestionPanel.js";
import { TodoPanel } from "./TodoPanel.js";
import { Markdown } from "./Markdown.js";
import { theme } from "../theme.js";

/** 最多渲染的消息条数。超出时旧消息折叠，避免 React 全量调和导致终端抖动。 */
const MAX_VISIBLE = 50;

interface Props {
  items: ChatItem[];
  /** agent 是否正在工作中。为 true 时，当前 assistant 文本仍在流式接收，
   *  跳过 Markdown 解析直接用纯文本渲染，避免 O(n^2) 解析开销。
   *  同时强制折叠历史消息（无视 showAll），防止抖动。 */
  busy?: boolean;
  /** 用户是否主动展开全部历史消息（Ctrl+E 切换）。busy 期间被忽略。 */
  showAll?: boolean;
  /** 当一个 ask_question item 处于 pending（未作答）时调用。已作答时 App 不传 onAnswer。 */
  onAnswerQuestion?: (
    questionId: string,
    params: { selected_labels: string[]; other_text?: string; cancelled?: boolean },
  ) => void;
  /** 当前 pending 的 question_id；用于决定哪一个 ask_question item 接收输入 */
  activeQuestionId?: string | null;
}

/** 主对话流：把 ChatItem 列表按角色渲染。
 *
 *  性能要点：
 *  1. 窗口渲染：只显示最近 MAX_VISIBLE 条，旧消息折叠为一行动态提示。
 *     避免 items 积累后每次 setItems 触发全量 React 调和 → Ink ANSI 重写 → 终端抖动。
 *  2. 连续 thought item 合并为一个视觉块 —— 只有第一个显示 "💭 thinking" 头部。
 *  3. 流式输出阶段：当前 assistant 文本还在不断增长时，跳过 Markdown 解析，
 *     直接用纯 Text 渲染 —— 否则每次 flush 都要 parseBlocks 解析全文，O(n^2)。
 *     done 事件后 busy 变 false，自动切回 Markdown 渲染。 */
export function EventStream({ items, busy, showAll, onAnswerQuestion, activeQuestionId }: Props) {
  // busy 期间强制折叠防抖动；空闲时尊重用户 Ctrl+E 选择
  const effectiveShowAll = !busy && showAll;

  // 窗口：只渲染尾部最多 MAX_VISIBLE 条
  const { visibleItems, hiddenCount } = useMemo(() => {
    if (effectiveShowAll || items.length <= MAX_VISIBLE) {
      return { visibleItems: items, hiddenCount: 0 };
    }
    return {
      visibleItems: items.slice(items.length - MAX_VISIBLE),
      hiddenCount: items.length - MAX_VISIBLE,
    };
  }, [items, effectiveShowAll]);

  // 找到当前正在流式增长的 assistant item 在 visibleItems 中的索引
  const streamingIdx = busy
    ? (() => {
        for (let i = visibleItems.length - 1; i >= 0; i--) {
          if (visibleItems[i].role === "assistant") return i;
        }
        return -1;
      })()
    : -1;

  return (
    <Box flexDirection="column">
      {hiddenCount > 0 && (
        <Box marginBottom={1}>
          <Text dimColor>
            ... 以上 {hiddenCount} 条消息已折叠（Ctrl+E 展开 / 再按折叠）
          </Text>
        </Box>
      )}
      {visibleItems.map((it, i) => {
        if (it.role === "thought") {
          const prevWasThought = i > 0 && visibleItems[i - 1].role === "thought";
          const nextIsThought = i + 1 < visibleItems.length && visibleItems[i + 1].role === "thought";
          return (
            <Box key={it.id} marginBottom={nextIsThought ? 0 : 1}>
              <ThoughtChunk text={it.text} showHeader={!prevWasThought} />
            </Box>
          );
        }
        return (
          <Box key={it.id} marginBottom={1}>
            {renderItem(it, onAnswerQuestion, activeQuestionId, i === streamingIdx)}
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * 单个思考文本块。text 在创建后永不修改（App.tsx 每次 flush 创建新 chunk），
 * React.memo 保证旧块永远不重渲染 —— Ink 只需往终端追加新行，零擦写开销。
 */
const ThoughtChunk = React.memo(function ThoughtChunk({ text, showHeader }: { text: string; showHeader: boolean }) {
  return (
    <Box flexDirection="column" paddingLeft={2}>
      {showHeader && (
        <Box>
          <Text dimColor italic>💭 thinking</Text>
        </Box>
      )}
      <Box>
        <Text dimColor>{text}</Text>
      </Box>
    </Box>
  );
});

function renderItem(
  item: ChatItem,
  onAnswerQuestion?: Props["onAnswerQuestion"],
  activeQuestionId?: string | null,
  streaming?: boolean,
): React.ReactElement {
  if (item.role === "user") {
    // 用 Pane 给 user 消息加一条蓝色顶 Divider
    return (
      <Pane color={theme.accent}>
        <Box>
          <Text color={theme.accent} bold>you  </Text>
          <Text>{item.text}</Text>
        </Box>
      </Pane>
    );
  }
  if (item.role === "assistant") {
    // 流式接收中：纯文本渲染，跳过 Markdown 解析（O(n^2) → O(1)）
    // done 后 busy=false → streaming=false → 切回 Markdown
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <Box>
          <Text color={theme.agent} bold>cbagent  </Text>
        </Box>
        {streaming ? (
          <Text>{item.text}</Text>
        ) : (
          <Markdown text={item.text} />
        )}
      </Box>
    );
  }
  if (item.role === "tool") {
    return <ToolBlock item={item} />;
  }
  if (item.role === "todo") {
    return <TodoPanel items={item.todoItems ?? []} />;
  }
  if (item.role === "plan") {
    return <PlanPanel item={item} />;
  }
  if (item.role === "system") {
    return <SystemMessage text={item.text} />;
  }
  if (item.role === "ask_question") {
    // 仅给当前 active 的问题接 onAnswer；其他（已答 / 旧的）传 undefined → 转纯展示
    const isActive = !!activeQuestionId && item.questionId === activeQuestionId && !item.answered;
    return (
      <AskQuestionRow
        item={item}
        isActive={isActive}
        onAnswerQuestion={onAnswerQuestion}
      />
    );
  }
  return <Text dimColor>{item.text}</Text>;
}

function PlanPanel({ item }: { item: ChatItem }) {
  const status = item.planStatus ?? "idle";
  const color =
    status === "approved" ? theme.success :
    status === "rejected" ? theme.error :
    status === "pending" ? theme.warning :
    theme.info;
  return (
    <Pane color={color}>
      <Box flexDirection="column">
        <Box>
          <Text color={color} bold>plan</Text>
          {item.planRevision ? <Text dimColor>  rev {item.planRevision}</Text> : null}
          <Text dimColor>  {status}</Text>
        </Box>
        <Box marginTop={1} flexDirection="column">
          <Markdown text={item.text} />
        </Box>
        {status === "pending" ? (
          <Box marginTop={1}>
            <Text dimColor>/plan approve  or  /plan reject &lt;feedback&gt;</Text>
          </Box>
        ) : null}
      </Box>
    </Pane>
  );
}

function SystemMessage({ text }: { text: string }) {
  const mcp = parseMcpStatus(text);
  if (mcp) return <MCPStatusCard data={mcp} />;

  const skills = parseSkillList(text);
  if (skills) return <SkillListCard data={skills} />;

  return (
    <Pane color={theme.info}>
      <Box flexDirection="column">
        <Text color={theme.info} bold>system</Text>
        <Markdown text={text} />
      </Box>
    </Pane>
  );
}

type ParsedMCPStatus = {
  state: string;
  connected: number;
  total: number;
  failed: number;
  servers: Array<{ name: string; status: string; detail?: string }>;
};

function parseMcpStatus(text: string): ParsedMCPStatus | null {
  const lines = text.trim().split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const head = /^MCP 状态：(.+?)（(\d+)\/(\d+) connected，(\d+) failed）$/.exec(lines[0] ?? "");
  if (!head) return null;
  return {
    state: head[1],
    connected: Number(head[2]),
    total: Number(head[3]),
    failed: Number(head[4]),
    servers: lines.slice(1).map((line) => {
      const match = /^[•\-]\s*([^:]+):\s*([^\s(]+)(?:\s*\((.*)\))?$/.exec(line);
      if (!match) return { name: line, status: "unknown" };
      return { name: match[1], status: match[2], detail: match[3] };
    }),
  };
}

function MCPStatusCard({ data }: { data: ParsedMCPStatus }) {
  const ready = data.state === "ready" || data.connected === data.total;
  const color = data.failed > 0 ? theme.error : ready ? theme.success : theme.info;
  return (
    <Pane color={color}>
      <Box flexDirection="column">
        <Box>
          <Text color={color} bold>MCP </Text>
          <Text color={color} bold>{data.state}</Text>
          <Text dimColor>  {data.connected}/{data.total} connected</Text>
          {data.failed > 0 && <Text color={theme.error}>  {data.failed} failed</Text>}
        </Box>
        <Box flexDirection="column" marginTop={1}>
          {data.servers.map((server) => {
            const serverColor = server.status === "connected" ? theme.success : server.status === "error" ? theme.error : theme.warning;
            return (
              <Box key={server.name}>
                <Text color={serverColor}>● </Text>
                <Text bold>{server.name}</Text>
                <Text dimColor>  </Text>
                <Text color={serverColor}>{server.status}</Text>
                {server.detail && <Text dimColor>  {server.detail}</Text>}
              </Box>
            );
          })}
        </Box>
      </Box>
    </Pane>
  );
}

type ParsedSkillList = {
  count: number;
  skills: Array<{ name: string; description: string }>;
};

function parseSkillList(text: string): ParsedSkillList | null {
  const lines = text.trim().split(/\r?\n/);
  const head = /^已发现\s+(\d+)\s+个\s+Skill：$/.exec((lines[0] ?? "").trim());
  if (!head) return null;
  const skills = lines.slice(1).map((line) => {
    const match = /^\s*[-•]\s+([^:]+):\s*(.*)$/.exec(line);
    if (!match) return null;
    return { name: match[1].trim(), description: match[2].trim() };
  }).filter((item): item is { name: string; description: string } => item !== null);
  return { count: Number(head[1]), skills };
}

function SkillListCard({ data }: { data: ParsedSkillList }) {
  return (
    <Pane color={theme.primary}>
      <Box flexDirection="column">
        <Box>
          <Text color={theme.primary} bold>Skills </Text>
          <Text bold>{data.count}</Text>
          <Text dimColor> available</Text>
        </Box>
        <Box flexDirection="column" marginTop={1}>
          {data.skills.map((skill) => (
            <Box key={skill.name} flexDirection="column" marginBottom={1}>
              <Box>
                <Text color={theme.suggestion}>◆ </Text>
                <Text color={theme.suggestion} bold>{skill.name}</Text>
              </Box>
              {skill.description && (
                <Box paddingLeft={2}>
                  <Text dimColor>{skill.description}</Text>
                </Box>
              )}
            </Box>
          ))}
        </Box>
      </Box>
    </Pane>
  );
}

const AskQuestionRow = React.memo(function AskQuestionRow({
  item,
  isActive,
  onAnswerQuestion,
}: {
  item: ChatItem;
  isActive: boolean;
  onAnswerQuestion?: Props["onAnswerQuestion"];
}) {
  const onAnswer = React.useMemo(
    () =>
      isActive && onAnswerQuestion
        ? (p: { selected_labels: string[]; other_text?: string; cancelled?: boolean }) =>
            onAnswerQuestion(item.questionId!, p)
        : undefined,
    [isActive, onAnswerQuestion, item.questionId],
  );
  return <AskQuestionPanel item={item} onAnswer={onAnswer} />;
});
