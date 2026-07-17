# cb-agent OTUI

基于 **OpenTUI + Solid.js（Bun 运行时）** 的终端 UI，也是 cb-agent 唯一的本地交互界面。

## 运行

前置：[Bun](https://bun.sh) ≥ 1.3.14。

```bash
bun install
bun start          # = bun src/entry.tsx
bun dev            # 带 --watch
```

启动后自动 spawn `python run_agent.py --transport jsonrpc --memory-system light`。
Python 路径按 `CB_AGENT_PYTHON` → `../venv` → 系统 python 的顺序解析。

可转发的后端参数：

```bash
bun start --memory-system full
bun start --memory-system off
bun start --no-mcp
bun start --no-ctx
```

## 架构

```
entry.tsx                spawn Python + new Transport + createCliRenderer + render
  └─ app.tsx             Provider 栈 + 三栏布局（消息流+输入 | Sidebar，底部 Footer）
       ├─ context/
       │   ├─ theme.tsx       注入暗色主题
       │   ├─ transport.tsx   持有 Transport（与框架无关）
       │   └─ session.tsx     createStore 全局状态 + AgentEvent→状态 reducer（核心）
       └─ components/         MessageList(scrollbox) / AssistantMessage(markdown) /
                              ToolBlock / ReasoningBlock / TodoPanel / QuestionPanel /
                              Prompt / SlashCommandPicker / Sidebar / Footer / ActivityPanel
```

`transport.ts` / `types.ts` 与框架无关，使用 `node:*` 和 JSON-RPC 管理 Python 后端。

关键设计：OpenTUI 的 scrollbox + Solid 的 `createStore` 细粒度更新，使流式高频 `text_delta`
可以直接 setStore 追加，无需在前端做全局消息窗口化。

## 依赖

- `@opentui/core` / `@opentui/solid` / `@opentui/keymap` `0.3.4`
- `solid-js` `1.9.10`
- `bunfig.toml` 的 `preload = ["@opentui/solid/preload"]` 必须保留——它注册 Solid 的 babel JSX
  转换，缺了会报 `jsxDEV not found`。
