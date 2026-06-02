# 多会话隔离与 TUI 切换技术报告

## 0. 背景

上一轮跨轮工作上下文改造已经把每轮对话写入项目级 `.cbagent/sessions/`，但当时只有一个 `active_session_id`，启动时自动恢复最近 active session。

这能解决“重启后丢上下文”，但还不能解决“多个任务/多段对话隔离”的问题：如果用户想把 A 任务和 B 任务分开，原实现只能继续写同一个 active session，或者 `/clear` 删除当前会话。

本次改造目标是：

1. 保留自动恢复 active session。
2. 支持列出、新建、切换多个本地会话。
3. 切换时只恢复目标会话的 `history/state`，不串线。
4. CLI 和 TUI 都有对应入口。
5. 不把跨轮历史伪装成 `role=tool`，继续只恢复普通 user/assistant 文本和 `【工作记录】`。

---

## 1. 存储模型

目录结构保持项目级：

```text
.cbagent/
  sessions/
    index.json
    session_20260602_120000_ab12cd34/
      transcript.jsonl
      state.json
    session_20260602_130000_ef56ab78/
      transcript.jsonl
      state.json
```

`index.json` 仍只保存 active 指针：

```json
{
  "active_session_id": "session_20260602_120000_ab12cd34",
  "updated_at": "..."
}
```

会话列表不存进 `index.json`，而是扫描 `.cbagent/sessions/session_*` 子目录生成。这样有三个好处：

1. `index.json` 很小，只承担“当前激活会话”职责。
2. 手工删除某个 session 目录后，不需要维护一份复杂索引。
3. 每个会话的 transcript/state 仍天然隔离在自己的目录里。

---

## 2. LocalSessionStore 新接口

新增接口集中在 `agent/work_context.py` 的 `LocalSessionStore`：

```python
list_sessions() -> List[Dict[str, Any]]
create_session() -> Dict[str, Any]
switch_session(session_id: str) -> Dict[str, Any]
current_session_summary() -> Optional[Dict[str, Any]]
```

### 2.1 list_sessions

`list_sessions()` 只返回轻量摘要：

```json
{
  "session_id": "session_20260602_120000_ab12cd34",
  "created_at": "...",
  "updated_at": "...",
  "turn_count": 3,
  "active_task": "...",
  "rolling_summary": "...",
  "is_active": true
}
```

它不会读取 transcript 全文，也不会展开 trace。这样 TUI 打开会话面板时不会把大量历史内容载入 UI。

### 2.2 create_session

`create_session()` 创建一个新的 `session_*` 目录，初始化空 `state.json`，并立即写 `index.json` 把它设为 active。

`AgentSession.create_session()` 会额外清空内存 `history`，确保新会话不会继承旧会话上下文。

### 2.3 switch_session

`switch_session(session_id)` 做三件事：

1. 校验 session id 只允许 `session_YYYYMMDD_HHMMSS_8hex` 形式。
2. 切换 `active_session_id` 并加载目标目录的 `state.json`。
3. 更新 `index.json`。

随后 `AgentSession.switch_session()` 会调用 `load_latest_history()`，只从目标 session 的 `transcript.jsonl` 恢复普通 history。

---

## 3. 安全边界

会话切换和删除都涉及文件路径，所以新增了路径安全校验：

- `_is_valid_session_id(session_id)`
  - 只接受 `session_\d{8}_\d{6}_[0-9a-f]{8}`
  - 拒绝 `../outside`、绝对路径、任意目录名

- `_assert_safe_session_dir(target)`
  - 在 `clear_active_session()` 调 `shutil.rmtree()` 前确认目标目录位于 `sessions` 根目录下

这保证 JSON-RPC 的 `session.switch` 不会变成任意路径读取接口，`/clear` 也不会因为损坏的 index 删除 sessions 目录外的内容。

---

## 4. AgentSession 语义

`agent/session.py` 新增：

```python
export_history()
list_sessions()
current_session_payload()
create_session()
switch_session(session_id)
```

其中 `export_history()` 只导出：

```json
{
  "role": "user|assistant|system",
  "content": "...",
  "kind": "work_record|null"
}
```

它故意不导出 `tool_calls`、`tool_call_id`、`role=tool`。这是为了保持跨轮恢复语义：恢复的是对话和工作记录，不是 OpenAI tool calling 协议现场。

---

## 5. JSON-RPC 接口

`agent/transport/gateway.py` 新增：

| method | params | result |
| --- | --- | --- |
| `session.list_sessions` | `{}` | `{sessions, current}` |
| `session.create` | `{}` | `{session, history: []}` |
| `session.switch` | `{session_id}` | `{session, history}` |

另外，`gateway_ready` 事件现在会带上当前 active session 和已恢复 history：

```json
{
  "type": "gateway_ready",
  "model": "...",
  "session": {...},
  "history": [...]
}
```

这样 TUI 启动后不仅模型上下文恢复，界面也能看到当前会话的最近对话。

### busy 防护

`session.create` 和 `session.switch` 在 gateway busy 时会返回 `-32001 session busy`。

原因是：如果一轮 chat 正在运行，强行切换 active session，可能导致本轮从旧会话读取上下文，却在新会话目录落盘。busy 防护能避免这种跨目录写入竞态。

---

## 6. CLI 入口

`run_agent.py` 新增命令：

```text
/sessions
/new
/switch <session_id>
```

语义：

- `/sessions`
  - 列出所有本地 session 摘要
  - `*` 标记 active session

- `/new`
  - 创建新 session
  - 清空内存 history
  - 后续对话写入新目录

- `/switch <session_id>`
  - 切换 active session
  - 恢复目标 session 最近 history

`/clear` 语义保持彻底删除当前 active session 和 `index.json`。

---

## 7. TUI 入口

TUI 改动包括：

- `ui-tui/src/transport.ts`
  - `listSessions()`
  - `createSession()`
  - `switchSession(session_id)`

- `ui-tui/src/commands.ts`
  - `/sessions`
  - `/new`
  - `/switch <id>`

- `ui-tui/src/components/SessionSwitcher.tsx`
  - 可见会话切换面板
  - `Enter` 切换
  - `n` 新建
  - `r` 刷新
  - `Esc` 关闭

- `ui-tui/src/App.tsx`
  - `gateway_ready.history` 首屏恢复
  - 切换成功后用返回的 `history` 替换当前 `items`
  - 会话面板打开时禁用 PromptInput，避免按键路由冲突

状态栏会显示当前 session 的短 id，并提示 `/sessions to switch`。

---

## 8. 为什么 TUI 切换时替换 items

切换会话时，UI 不能把目标会话 history 追加到当前 items 后面，否则视觉上 A/B 两个会话会混在一起。

因此切换成功后执行的是：

```text
setItems(restoredHistoryToItems(payload.history))
```

`【工作记录】` 在 UI 中渲染成 system 行，避免和 assistant 的最终回答混淆；但在后端 history 中它仍是普通 assistant message，以便 ContextBuilder 继续使用。

---

## 9. 测试覆盖

新增/更新测试：

- `test/test_work_context.py`
  - 多个 session 目录能 list/create/switch
  - 切换后只恢复目标 transcript
  - 非法 session id 被拒绝

- `test/test_session_renderer.py`
  - `AgentSession.create_session()` 清空内存 history
  - `AgentSession.switch_session()` 恢复目标会话 history，不串入其它会话

- `test/test_transport.py`
  - gateway 支持 `session.list_sessions`
  - gateway 支持 `session.create`
  - gateway 支持 `session.switch`
  - switch 返回普通 history

- `ui-tui/src/__tests__/commands.test.ts`
  - `/sessions` 打开会话面板
  - `/new` 调 createSession 并应用 payload
  - `/switch <id>` 调 switchSession 并应用 payload

- `ui-tui/src/__tests__/transport.test.ts`
  - TUI transport 正确序列化三个新 RPC

已跑验证：

```text
venv/python.exe test/test_work_context.py
venv/python.exe test/test_session_renderer.py
venv/python.exe test/test_transport.py
npm test
npm run build
```

---

## 10. 后续方向

当前实现只支持列出、新建、切换、清理 active session。后续可以继续加：

- 删除指定非 active session
- 归档 session
- 自动生成会话标题
- 按 active_task/summary 搜索会话
- 在 TUI 面板中显示更丰富的文件/命令摘要

---

## 11. 总结

本次改造把 `.cbagent/sessions` 从“单 active 自动恢复”升级成“项目级多会话存储”。核心策略是：每个 session 独立目录保存 transcript/state，`index.json` 只保存当前 active 指针；切换时只重载目标目录的 state/history，不合并、不伪造 tool 消息、不跨会话串线。
