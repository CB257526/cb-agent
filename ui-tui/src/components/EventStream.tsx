import React from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { ToolBlock } from "./ToolBlock.js";
import { Pane } from "./Pane.js";
import { AskQuestionPanel } from "./AskQuestionPanel.js";
import { TodoPanel } from "./TodoPanel.js";
import { Markdown } from "./Markdown.js";
import { theme } from "../theme.js";

interface Props {
  items: ChatItem[];
  /** agent 是否正在工作中。为 true 时，当前 assistant 文本仍在流式接收，
   *  跳过 Markdown 解析直接用纯文本渲染，避免 O(n^2) 解析开销。 */
  busy?: boolean;
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
 *  性能要点：连续 thought item 合并为一个视觉块 —— 只有第一个显示
 *  "💭 thinking" 头部，后续的只显示纯文本。每个 thought chunk 是不可变的
 *  （App.tsx 每次 flush 创建新 chunk，从不修改旧 chunk），所以 React.memo
 *  让 Ink 跳过所有旧块的调和/布局/ANSI 输出，只追加新行到终端。
 *
 *  流式输出阶段：当前 assistant 文本还在不断增长时，跳过 Markdown 解析，
 *  直接用纯 Text 渲染 —— 否则每次 flush 都要 parseBlocks 解析全文，O(n^2)。
 *  done 事件后 busy 变 false，自动切回 Markdown 渲染。 */
export function EventStream({ items, busy, onAnswerQuestion, activeQuestionId }: Props) {
  // 找到当前正在流式增长的 assistant item 索引（done 后 busy=false，所有项都用 Markdown）
  const streamingIdx = busy
    ? (() => { for (let i = items.length - 1; i >= 0; i--) { if (items[i].role === "assistant") return i; } return -1; })()
    : -1;

  return (
    <Box flexDirection="column">
      {items.map((it, i) => {
        if (it.role === "thought") {
          const prevWasThought = i > 0 && items[i - 1].role === "thought";
          const nextIsThought = i + 1 < items.length && items[i + 1].role === "thought";
          // 连续的 thought chunk：头部只在第一块显示，间距只在最后一块加
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
