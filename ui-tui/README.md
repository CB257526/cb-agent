# cb-agent TUI（Stage 5b）

Claude Code 风格的终端 UI，用 [Ink](https://github.com/vadimdemedes/ink)（React for CLI）渲染。
作为子进程 spawn `python run_agent.py --transport jsonrpc`，把 stdio JSON-RPC 事件流转成
分区、可折叠工具块、底部输入框的 TUI。

**这个目录跟 [`agent/`](../agent) 是物理隔离的**：Python 端不知道也不在乎 UI 的存在，UI 端只看
[Stage 5a 文档](../note/Stage5a%20stdio%20JSON-RPC%20网关技术报告.md)定义的协议。换 UI 框架（比如改 Textual / web）只要协议对上，agent 一行不动。

---

## 快速开始

```bash
# 第一次：装依赖
cd ui-tui
npm install

# 跑起来
npm start
```

启动后会自动 spawn Python 后端（默认从 `../venv/python.exe` 找解释器）。

如果要使用 Buddy 宠物，先在项目根目录 `.env` 开启：

```env
FEATURE_BUDDY=1
```

然后重启 TUI，输入：

```text
/buddy hatch
```

退出：

- `Ctrl-C`（agent 工作时）→ 中断当前 chat
- `Ctrl-C`（空闲时）→ 退出 TUI 并通知后端关闭

---

## 目录

```
ui-tui/
├── package.json
├── tsconfig.json
├── README.md            ← 本文件
└── src/
    ├── entry.tsx        ← 启动入口：解析 python 路径、spawn、render
    ├── transport.ts     ← stdio JSON-RPC 客户端（NDJSON 行缓冲解析）
    ├── App.tsx          ← Ink 主组件：事件 → ChatItem 状态机 + 键盘
    ├── types.ts         ← cb-agent 事件类型 mirror
    ├── buddy/           ← Buddy 输入框旁 sprite 和 /buddy 卡片渲染
    ├── components/
    │   ├── EventStream.tsx   对话流
    │   ├── ToolBlock.tsx     工具块
    │   ├── StatusBar.tsx     底部状态栏
    │   └── PromptInput.tsx   输入框
    ├── __tests__/       ← vitest 单测
    └── __smoke__/       ← 真 spawn 后端的 smoke
```

---

## 协议层

跟 [`agent/transport/`](../agent/transport) 对接，详细设计在
[Stage 5a 报告](../note/Stage5a%20stdio%20JSON-RPC%20网关技术报告.md)。简要：

- **stdout**：NDJSON，一行一条 JSON-RPC 消息
- **stderr**：Python 端诊断输出，UI 把它写到 `~/.cb-agent/logs/gateway-<ts>.log`，**不**显示在屏幕上
- **stdin**：UI → 后端的 RPC 请求

UI 收到的两类消息：

| 类型 | 形态 | 用途 |
|---|---|---|
| event（notification） | `{"jsonrpc":"2.0","method":"event","params":{...}}` | agent 事件流 |
| response | `{"jsonrpc":"2.0","id":"r1","result":...}` | RPC 应答（UI 主要用 event 流，response 只用于错误判断） |

UI 发出的 RPC：

```ts
transport.sendPrompt("帮我看看 X")  // → prompt.submit
transport.cancel()                   // → session.cancel
transport.clearHistory()             // → session.clear_history
transport.getBuddyState()             // → buddy.get_state
transport.runBuddyCommand("pet")      // → buddy.command
transport.quit()                     // → session.quit
```

---

## 配置

### Python 解释器路径

按优先级解析：

1. 环境变量 `CB_AGENT_PYTHON`
2. `../venv/python.exe`（Windows）/ `../venv/bin/python`（POSIX）/ `../venv/Scripts/python.exe`
3. 系统 `python`

```bash
# Linux/macOS 自定义示例
CB_AGENT_PYTHON=/path/to/python npm start
```

### 危险权限模式

TUI 默认仍会让后端 BashTool 保持权限确认和高危命令拦截。如果你确认当前模型、提示词和工作目录都可信，可以用环境变量让 TUI spawn 的 Python 后端追加 `--dangerously-skip-permissions`：

```bash
CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS=1 npm start
```

开启后 BashTool 会跳过权限确认、非只读检查和高危命令拦截，agent 可以直接执行任意 shell 命令。工具结果里的 `permission.dangerously_skipped=true` 会标记这次放行来自危险模式。不要在共享服务器、公网服务、QQ 群聊或不可信模型/提示词场景开启。

### 后端日志位置

`~/.cb-agent/logs/gateway-<timestamp>.log`。UI 启动时新建一个文件，把 Python stderr 全写过去。
协议解析失败时 UI 会给出文件路径让用户去看。

### Buddy 宠物

Buddy 默认关闭。开启方式：

```env
FEATURE_BUDDY=1
```

可用命令：

| 命令 | 说明 |
|---|---|
| `/buddy` 或 `/buddy status` | 查看当前 Buddy 状态 |
| `/buddy hatch` | 第一次孵化 Buddy |
| `/buddy rehatch` | 重新孵化并替换当前 Buddy |
| `/buddy pet` | 摸摸 Buddy，触发心心动画和气泡反应 |
| `/buddy mute` 或 `/buddy off` | 静音并隐藏输入框旁 Buddy |
| `/buddy unmute` 或 `/buddy on` | 取消静音并重新显示 |

TUI 启动后会调用 `buddy.get_state` 拉取当前状态；执行 `/buddy` 子命令会调用 `buddy.command`，后端状态变化时再广播 `buddy_updated`。Buddy 配置存储在 `~/.cbagent/buddy.json`，不写入当前聊天 history。

---

## 开发

```bash
npm run dev      # tsx watch，改 src/ 自动重启
npm run build    # 编译到 dist/
npm test         # vitest，跑 src/__tests__/
npm run smoke    # 真 spawn Python 后端，验证握手（不打 LLM）
```

### 类型检查

```bash
npx tsc --noEmit
```

### 项目特意保持的简化

- **不做重连**：Python 崩了 UI 直接退，让用户重启。Ink 也没必要在崩溃后保留半截状态
- **不做 RPC 超时 / 队列**：cb-agent 单 session，prompt.submit 立即 ack；UI 等事件流就行
- **不显示 reasoning 内容**：DeepSeek-R 系列的 `reasoning_delta` 默认丢弃，避免占屏；将来要看再加面板

---

## 已知小坑

### 1. busy 状态下用户连发 prompt

UI 在 busy 时会禁用输入框（显示 "（agent 正在工作，等待结束）"）。但如果用户用脚本/macro 强行
往 stdin 灌 prompt，后端会回 `-32001 session busy` 错误响应——UI 静默忽略（不弹窗），因为这通常
是误操作。

### 2. 工具结果只显示前 600 字

cb-agent 工具结果（尤其是 file_read、search、bash）可能很长。屏幕上截前 600，剩下的用一行
"... [+N chars truncated, see ~/.cb-agent/logs]" 提示。完整内容**不在** stderr 日志里——agent 那边
没把工具结果写出来。如果要看完整结果，需要改 agent 加日志，或者扩展 ToolBlock 支持滚动查看（
TODO，Stage 5c 候选）。

### 3. tool_complete 按 name 匹配 tool_start 而不是 call_id

cb-agent 的 `ToolStart` 事件带 call_id，但 UI 内部为了简单是按 "最近未完成 + 同 name" 找回对应的
tool item。**目前 cb-agent executor 里每轮 tool calls 都是顺序串行 emit**（即使工具本身并发执行，
事件 emit 是单线程的），所以这种匹配是对的。如果将来事件 emit 也并发，要改成按 call_id 匹配。

---

## 历史

- 2026-01：Stage 5a Python 端 transport 完成（177 个测试全绿）
- 2026-01：Stage 5b 本目录搭起来，~600 行 TS/TSX，7 个 vitest 单测 + transport smoke 通过
