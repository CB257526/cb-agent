# `/compact` 上下文压缩命令技术报告

> 本报告对应本次新增的显式上下文压缩命令。
> 关键源代码：[agent/session.py](../agent/session.py)、[agent/work_context.py](../agent/work_context.py)、[agent/transport/gateway.py](../agent/transport/gateway.py)、[ui-tui/src/commands.ts](../ui-tui/src/commands.ts)。

---

## 0. 背景

前一轮改造已经把跨轮工作信息写入 `self.history` 和项目级 `.cbagent/sessions/`，解决了“下一轮不知道看过哪些文件、跑过哪些命令”的问题。

但长期会话会出现另一个问题：`history` 中的普通对话、`【工作记录】`、滚动 state 会持续占用后续 prompt。虽然 `history_max_messages` 有窗口限制，但窗口裁剪是被动的，用户无法在任务进行到某个阶段时主动“把旧上下文压成摘要，然后继续工作”。

因此新增 `/compact`：

1. 立即压缩当前 active 会话的内存 `history` 和本地 `state.json`。
2. 后续 prompt 只保留一条 `【上下文压缩】` 摘要和最近一轮普通 user/assistant 对话。
3. 不删除、不重写 `transcript.jsonl`，旧记录仍作为审计材料留在磁盘。
4. TUI 当前屏幕不重绘，只追加一条系统提示，避免用户视觉上“旧对话突然消失”。

---

## 1. 核心语义

`/compact` 和 `/clear` 是两种完全不同的操作：

| 命令 | 内存 history | 本地 transcript | 本地恢复语义 |
|---|---|---|---|
| `/clear` | 清空 | 删除当前 session 文件 | 重启不会恢复旧上下文 |
| `/compact` | 压缩成摘要 + 最近一轮 | 保留原始 transcript | 重启从 compact 快照继续 |

这次实现中特别保留了两个安全边界：

1. `【上下文压缩】` 仍然是普通 assistant message，metadata 为 `{"kind": "compact_record"}`，不会伪装成 `role="tool"`。
2. compact 摘要不会读取完整工具输出，也不会保存完整文件正文或完整 bash stdout。

这样可以继续遵守 OpenAI tool calling 协议：`role=tool` 只存在于同一轮工具循环的 `messages` 中，不跨轮恢复。

---

## 2. 后端流程

后端入口是 `AgentSession.compact_context()`。

执行顺序：

1. 读取当前 `self.history` 条数，作为 `before_messages`。
2. 从 `LocalSessionStore.state_text()` 读取当前会话的滚动状态。
3. 如果 `history` 和 state 都为空，则返回 `no_op=true`。
4. 调用 `_make_compact_summary()` 生成摘要。
5. 用 `make_compact_record_message()` 包装成 `【上下文压缩】` assistant message。
6. 用 `_latest_plain_turn_messages()` 保留最近一轮普通 user/assistant 对话。
7. 将内存 `self.history` 改为：

```text
[
  assistant(kind=compact_record, content="【上下文压缩】..."),
  user(...最近一轮用户输入...),
  assistant(...最近一轮最终回答...)
]
```

8. 如果启用了 `session_store`，调用 `save_compaction()` 落盘。
9. 返回 `{session, history, summary, before_messages, after_messages, persisted, no_op}`。

这里没有调用 `llm.think()`，也不会 emit `TextDelta/Done`。它是管理 RPC，不是一轮普通助手回答。

---

## 3. 摘要生成

摘要生成优先使用静默 LLM，总长度目标不超过 1200 字。

输入包括两部分：

- 当前内存 `history` 的短渲染文本；
- `state.json` 的滚动状态、当前任务、关键结论、待办/阻塞等结构化信息。

系统提示要求保留：

- 当前任务；
- 用户偏好；
- 关键结论；
- 已读文件；
- 已改文件；
- 最近命令；
- 待办/阻塞。

如果 LLM 客户端不可用、模型缺失、调用异常或返回空内容，则退回 `_rule_compact_summary()`。规则兜底只重组已有 `history/state`，不推断新事实，保证 `/compact` 不会因为压缩器异常阻断主流程。

---

## 4. 本地持久化

本地会话目录新增两个 compact 文件：

```text
.cbagent/
  sessions/
    index.json
    session_xxx/
      transcript.jsonl
      state.json
      compact.json
      compactions.jsonl
```

### 4.1 compact.json

`compact.json` 保存最新一次 compact 快照：

```json
{
  "ts": "...",
  "session_id": "session_xxx",
  "summary": "【上下文压缩】...",
  "transcript_offset": 12,
  "history": [
    { "role": "assistant", "content": "【上下文压缩】...", "kind": "compact_record" },
    { "role": "user", "content": "...", "kind": null },
    { "role": "assistant", "content": "...", "kind": null }
  ],
  "before_messages": 12,
  "after_messages": 3
}
```

`transcript_offset` 是压缩发生时 `transcript.jsonl` 已有的轮次数。恢复时只读取 offset 之后的新 transcript 行，旧行不再注入 prompt。

### 4.2 compactions.jsonl

`compactions.jsonl` 追加记录每一次 compact 事件。它用于审计，不参与常规恢复。这样可以追踪每次压缩发生的时间、摘要和消息数量变化。

### 4.3 state.json

compact 后会更新 `state.json`：

- `rolling_summary` 替换为 compact 摘要；
- `compacted_at` 记录压缩时间；
- `compact_count` 累加；
- `compact_transcript_offset` 记录 transcript offset；
- `files_seen`、`files_modified`、`recent_commands`、`decisions`、`pending` 做有界裁剪。

这让 state 继续作为 P1 `[State]` 提供长期工作状态，但不会无限增长。

---

## 5. 恢复逻辑

`LocalSessionStore.load_latest_history()` 增加 compact 优先恢复：

1. 如果没有 `compact.json`，仍按旧逻辑从 `transcript.jsonl` 恢复最近若干条 user/final/work_record。
2. 如果存在 `compact.json`，先恢复 compact 快照里的轻量 `history`。
3. 再读取 `transcript_offset` 之后新增的 transcript 轮次。
4. 最后用 `_trim_restored_history()` 裁剪恢复窗口，并尽量保留最近的 `compact_record` 锚点。

这样旧 transcript 仍留在磁盘，但不会在重启或切换会话时重新挤进 prompt。

---

## 6. Gateway 与 TUI

Gateway 新增 `session.compact` RPC：

- busy 时返回 `_ERR_BUSY`；
- 空上下文返回 `no_op=true`；
- 成功时返回 compact payload；
- 异常时返回 `_ERR_INTERNAL`。

TUI 新增 `/compact` 命令：

- `Transport.compactSession()` 发送 `session.compact`；
- `/help` 和命令过滤能看到 `/compact`；
- handler 不调用 `applySessionPayload()`；
- handler 不清空 items、不重绘旧屏幕；
- 只追加系统提示，例如：

```text
已压缩上下文：history 12 -> 3，下轮将使用摘要继续（已落盘）。
```

会话恢复或切换时，`compact_record` 在 UI 中按 system 行渲染，避免和普通助手回答混淆。

---

## 7. 测试覆盖

本次新增和调整的测试包括：

- `test/test_session_renderer.py`
  - 验证 `compact_context()` 会压缩内存 history；
  - 验证下一轮构造的 `[Context]` 能看到 `【上下文压缩】`。

- `test/test_work_context.py`
  - 验证 compact 后不删除 `transcript.jsonl`；
  - 验证写入 `compact.json/compactions.jsonl`；
  - 验证重启恢复时从 compact 锚点和 compact 后新轮次继续。

- `test/test_transport.py`
  - 验证 Gateway `session.compact` 返回 payload，并写入 compact 快照。

- `ui-tui/src/__tests__/commands.test.ts`
  - 验证 `/compact` 可被命令过滤和精确查找；
  - 验证 `/help` 包含 `/compact`；
  - 验证 handler 只追加系统提示，不重绘 history；
  - 验证 no-op 提示。

- `ui-tui/src/__tests__/transport.test.ts`
  - 验证 `Transport.compactSession()` 序列化为 JSON-RPC method `session.compact`。

---

## 8. 风险与后续方向

当前实现保留的是“最新 compact 快照 + compact 后新轮次”。这适合释放 prompt，但不是磁盘瘦身工具，因为原始 `transcript.jsonl` 仍保留审计。

后续可以继续扩展：

1. 在 TUI 会话面板显示某会话最近一次 compact 时间。
2. 给 `/compact` 增加可选参数，例如 `/compact --rule` 强制规则摘要。
3. 在 `compactions.jsonl` 中记录触发来源，例如 TUI、CLI 或自动策略。
4. 增加 compact 摘要质量检查，发现摘要缺少“待办/阻塞”等字段时自动补规则片段。

