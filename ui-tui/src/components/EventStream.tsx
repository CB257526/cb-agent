import React from "react";
import { Box, Text } from "ink";
import { ChatItem } from "../types.js";
import { ToolBlock } from "./ToolBlock.js";
import { Pane } from "./Pane.js";
import { AskQuestionPanel } from "./AskQuestionPanel.js";
import { TodoPanel } from "./TodoPanel.js";
import { theme } from "../theme.js";

interface Props {
  items: ChatItem[];
  /** 当一个 ask_question item 处于 pending（未作答）时调用。已作答时 App 不传 onAnswer。 */
  onAnswerQuestion?: (
    questionId: string,
    params: { selected_labels: string[]; other_text?: string; cancelled?: boolean },
  ) => void;
  /** 当前 pending 的 question_id；用于决定哪一个 ask_question item 接收输入 */
  activeQuestionId?: string | null;
}

/** 主对话流：把 ChatItem 列表按角色渲染。 */
export function EventStream({ items, onAnswerQuestion, activeQuestionId }: Props) {
  return (
    <Box flexDirection="column">
      {items.map((it) => (
        <Box key={it.id} marginBottom={1}>
          {renderItem(it, onAnswerQuestion, activeQuestionId)}
        </Box>
      ))}
    </Box>
  );
}

function renderItem(
  item: ChatItem,
  onAnswerQuestion?: Props["onAnswerQuestion"],
  activeQuestionId?: string | null,
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
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <Box>
          <Text color={theme.claude} bold>claude  </Text>
        </Box>
        <Box>
          <Text>{item.text}</Text>
        </Box>
      </Box>
    );
  }
  if (item.role === "tool") {
    return <ToolBlock item={item} />;
  }
  if (item.role === "todo") {
    return <TodoPanel items={item.todoItems ?? []} />;
  }
  if (item.role === "thought") {
    // dim 灰显的"思考流"块；折叠机制简单——长度过长会自然换行，前端不主动截
    return (
      <Box flexDirection="column" paddingLeft={2}>
        <Box>
          <Text dimColor italic>💭 thinking</Text>
        </Box>
        <Box>
          <Text dimColor>{item.text}</Text>
        </Box>
      </Box>
    );
  }
  if (item.role === "ask_question") {
    // 仅给当前 active 的问题接 onAnswer；其他（已答 / 旧的）传 undefined → 转纯展示
    const isActive = !!activeQuestionId && item.questionId === activeQuestionId && !item.answered;
    return (
      <AskQuestionPanel
        item={item}
        onAnswer={
          isActive && onAnswerQuestion
            ? (p) => onAnswerQuestion(item.questionId!, p)
            : undefined
        }
      />
    );
  }
  return <Text dimColor>{item.text}</Text>;
}
