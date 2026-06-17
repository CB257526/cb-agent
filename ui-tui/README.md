# cb-agent TUI（Stage 5b）

Claude Code 风格的终端 UI，用 [Ink](https://github.com/vadimdemedes/ink)（React for CLI）渲染。
作为子进程 spawn `python/python3 run_agent.py --transport jsonrpc`，把 stdio JSON-RPC 事件流转成
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

启动后会自动 spawn Python 后端（默认从 `../venv/python.exe`、`../venv/Scripts/python.exe` 或 `../venv/bin/python` 找解释器）。

桌宠通过 `/pet` 管理；TUI 只发送控制命令，桌面浮窗由 cb-agent 内置 Python runtime 自己处理。

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
- **stderr**：Python 端诊断输出，UI 把它写到 `.cbagent/logs/system/gateway-<ts>.log`，**不**显示在屏幕上
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
transport.getPetState()                // → pet.get_state
transport.runPetCommand("show")        // → pet.command
transport.quit()                     // → session.quit
```

---

## 配置

### Python 解释器路径

按优先级解析：

1. 环境变量 `CB_AGENT_PYTHON`
2. `../venv/python.exe`（Windows）/ `../venv/bin/python`（POSIX）/ `../venv/Scripts/python.exe`
3. Windows 兜底系统 `python`；Linux/macOS 兜底系统 `python3`

```bash
# Linux/macOS 自定义示例
CB_AGENT_PYTHON=/path/to/cbAgent/venv/bin/python npm start
```

Linux 服务器上建议显式设置 `CB_AGENT_PYTHON`，因为很多发行版只有 `python3` 命令，没有 `python` 命令。TUI 现在会在找不到虚拟环境时自动兜底 `python3`，但显式路径更容易排查部署问题。

### 剪贴板与附件

TUI 的 `Ctrl-V` 会优先识别剪贴板文件、文本和图片：文件会加入附件队列，文本会插入输入框，图片会保存为临时 PNG 附件。`/paste-image` 仍只用于显式读取图片。

Linux 下图片粘贴依赖桌面剪贴板工具：

| 环境 | 依赖 | 不满足时 |
|---|---|---|
| Wayland | `wl-paste`（`wl-clipboard`） | 使用 `/attach <path>` |
| X11 | `xclip` | 使用 `/attach <path>` |
| SSH/headless | 通常没有系统剪贴板 | 使用 `/attach <path>` |

如果只是服务器部署，不需要桌面环境；把文件上传到后端可读目录后，用 `/attach <path>` 是最稳定的入口。PDF、Word、txt 等文档附件会在后端用 MarkItDown 转为 Markdown 后进入本轮消息；图片和音频继续走原有 OCR/ASR 或视觉输入逻辑。

### 危险权限模式

TUI 默认仍会让后端 BashTool 保持权限确认和高危命令拦截。如果你确认当前模型、提示词和工作目录都可信，可以用环境变量让 TUI spawn 的 Python 后端追加 `--dangerously-skip-permissions`：

```bash
CBAGENT_DANGEROUSLY_SKIP_PERMISSIONS=1 npm start
```

开启后 BashTool 会跳过权限确认、非只读检查和高危命令拦截，agent 可以直接执行任意 shell 命令。工具结果里的 `permission.dangerously_skipped=true` 会标记这次放行来自危险模式。不要在共享服务器、公网服务、QQ 群聊或不可信模型/提示词场景开启。

### 后端日志位置

`.cbagent/logs/system/gateway-<timestamp>.log`。UI 启动时新建一个文件，把 Python stderr 全写过去。
协议解析失败时 UI 会给出文件路径让用户去看。

### 桌宠

TUI 启动后会调用 `pet.get_state` 拉取当前桌宠状态；执行 `/pet` 子命令会调用 `pet.command`，后端状态变化时广播 `pet_updated`。桌宠状态不写入当前聊天 history。

桌宠 runtime 是内置 Python sidecar，不需要 Rust、Node 或 Tauri 构建。它通过 WebView 内的 `pixi.js` + `easy-live2d` + Live2D Cubism Core 真实渲染 BongoCat Live2D 模型，并用全局键盘/鼠标事件驱动 BongoCat 同款参数；TUI 只负责发送 `/pet` 控制命令。

可用命令：

| 命令 | 说明 |
|---|---|
| `/pet` 或 `/pet status` | 查看 runtime、可见性、当前宠物和活动状态 |
| `/pet install <folder>` | 安装宠物包根目录；Live2D 包根目录含 `*.model3.json`，spritesheet 包根目录含 `pet.json` |
| `/pet list` | 列出已安装宠物、当前选择、显示名和本地库路径 |
| `/pet select <id>` | 选择并加载宠物 |
| `/pet uninstall <id>` | 卸载已安装宠物；`remove` / `delete` 也可用 |
| `/pet launch` | 启动轻量 Python runtime |
| `/pet show` / `/pet hide` | 显示或隐藏桌宠窗口 |
| `/pet quit` | 关闭 runtime |

免命令导入：把宠物包根目录直接放进项目级 `.cbagent\pet\` 或用户级 `~\.cbagent\pet\`，下一次 `/pet list`、`/pet status` 或 `/pet launch` 会自动扫描并复制到 `~\.cbagent\pets\`。`/pet launch` 会自动选中新发现的包。

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
"... [+N chars truncated, see .cbagent/logs]" 提示。完整内容**不在** stderr 日志里——agent 那边
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
