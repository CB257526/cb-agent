# cb-agent OTUI

基于 **OpenTUI + Solid.js（Bun 运行时）** 的终端 UI，外观对齐 opencode，替代旧的 Ink 版 `ui-tui/`。

## 为什么重构

旧 Ink 版有两个框架级顽疾，重写组件治不好：

- 流式输出时滚轮一滑就跳到终端最顶部、对话结束前无法下滑（Ink 没有真实滚动视口）。
- 长按 delete 无法连续删除（Ink 按键重复处理缺陷）。

OpenTUI 有真正的 `<scrollbox>`（独立屏幕缓冲、自管滚动状态）和原生 `<input>`，从根上修掉这两个问题。

## 运行

前置：[Bun](https://bun.sh) ≥ 1.3.14。

```bash
bun install
bun start          # = bun src/entry.tsx
bun dev            # 带 --watch
```

启动后自动 spawn `python run_agent.py --transport jsonrpc --memory-system light`。
Python 路径解析同旧版（`CB_AGENT_PYTHON` → `../venv` → 系统 python）。

## 架构

```
entry.tsx                spawn Python + new Transport + createCliRenderer + render
  └─ app.tsx             Provider 栈 + 三栏布局（消息流+输入 | Sidebar，底部 Footer）
       ├─ context/
       │   ├─ theme.tsx       注入暗色主题
       │   ├─ transport.tsx   持有 Transport（与框架无关，复用自旧版）
       │   └─ session.tsx     createStore 全局状态 + AgentEvent→状态 reducer（核心）
       └─ components/         MessageList(scrollbox) / AssistantMessage(markdown) /
                              ToolBlock / ReasoningBlock / TodoPanel / QuestionPanel /
                              Prompt / SlashCommandPicker / Sidebar / Footer / ActivityPanel
```

`transport.ts` / `types.ts` 与框架无关（纯 `node:*` + JSON-RPC），从旧 `ui-tui/` 原样复用，
所以后端契约不变、Python 端无需改动。

关键设计：OpenTUI 的 scrollbox + Solid 的 `createStore` 细粒度更新，使流式高频 `text_delta`
可以直接 setStore 追加，**不再需要旧版的自适应节流（60-200ms）和窗口化（只渲染 50 条）**。

## 依赖

- `@opentui/core` / `@opentui/solid` / `@opentui/keymap` `0.3.4`
- `solid-js` `1.9.10`
- `bunfig.toml` 的 `preload = ["@opentui/solid/preload"]` 必须保留——它注册 Solid 的 babel JSX
  转换，缺了会报 `jsxDEV not found`。
