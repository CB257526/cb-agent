# 轻量 Markdown 记忆系统技术报告

## 1. 背景

原项目的记忆系统以 `MemoryTool`、`RAGTool`、embedding、向量数据库和多模态 RAG 为核心。功能很完整，但默认启动会让新用户面对较重的依赖和环境变量配置：本地 embedding 模型、`zvec` / Qdrant、Neo4j、RAG 文件解析库等都可能成为运行门槛。

这次改造的目标不是删除旧能力，而是新增一条默认轻量路径：

- 默认 `--memory-system light` 使用 Markdown 文件作为记忆存储和召回来源。
- 旧向量记忆和 RAG 完整保留，但改为 `--memory-system full` 显式启用。
- `--memory-system off` 只保留 session/history/state，不启用任何长期记忆。
- TUI 后端启动参数显式传入 `--memory-system light`，保证终端界面默认走轻量安装路径。

## 2. 启动模式

| 模式 | Markdown 记忆 | 旧 `memory` 工具 | 旧 `rag` 工具 | 依赖特点 |
|---|---|---|---|---|
| `light` | 启用 | 不注册 | 不注册 | 不需要 embedding、RAG、向量库依赖 |
| `full` | 不启用 | 注册 | 注册 | 需要 full extras，保留旧完整能力 |
| `off` | 不启用 | 不注册 | 不注册 | 只使用会话历史和本地 session state |

`run_agent.py` 和 `agent_run_basic.py` 都新增了 `--memory-system light|full|off` 参数，默认值是 `light`。TUI 的 `ui-tui/src/transport.ts` 使用固定后端参数：

```ts
["run_agent.py", "--transport", "jsonrpc", "--memory-system", "light"]
```

## 3. Markdown 记忆文件布局

轻量记忆由 `context/markdown_memory.py` 提供，包含两级目录：

| 级别 | 默认路径 | 适合保存 |
|---|---|---|
| global | `~/.cbagent/memory/` | 用户长期偏好、跨项目事实、反馈 |
| project | `<cb-agent 项目根>/.cbagent/memory/` | 当前项目的事实、约定、进展、参考 |

每个目录包含一个 `MEMORY.md` 索引文件。首次运行 light 模式时只创建目录和默认索引模板，不自动写入业务记忆，避免制造“伪记忆”。

具体记忆文件是同目录下其它 `.md` 文件，建议使用最小 YAML frontmatter：

```markdown
---
name: 用户偏好
description: 用户希望回答保持中文并附验证命令
type: user
scope: global
---

用户偏好：回答使用中文，必要时列出验证命令。
```

支持的 `type` 是 `user|feedback|project|reference`，`scope` 是 `global|project`。

## 4. 召回策略

轻量记忆完全不使用 embedding、向量库或 LLM rerank。召回流程如下：

1. 扫描 global 和 project 两级 `MEMORY.md` 索引。
2. 扫描同目录下其它 `.md` 记忆文件。
3. 解析最小 frontmatter：`name`、`description`、`type`、`scope`。
4. 构造可检索文本：名称、描述、索引描述、类型、正文。
5. 用 token Jaccard 和关键词重叠做相关性打分。
6. 将高价值状态记忆注入 `[State]`，将与当前 query 相关的记忆注入 `[Evidence]`。

为避免 Markdown 文件撑爆上下文，读取阶段限制单文件最大读取量，输出到上下文前也会按字符裁剪，并继续经过 `ContextBuilder` 的 token 预算筛选。

## 5. ContextBuilder 改造

`context/builder.py` 的关键变化：

- 移除了 `tools.tools.memory_tool` / `tools.tools.rag_tool` 的顶层运行时 import。
- 旧工具类型只在 `TYPE_CHECKING` 下引用，light 安装导入 `context.builder` 不会触碰 RAG 依赖。
- 新增 `md_memory_provider` 可选依赖。
- Gather 顺序调整为：
  1. system instructions
  2. `LocalSessionStore.state.json` 注入的本地 session state
  3. Markdown memory state / related
  4. full memory state / related
  5. full RAG
  6. history
  7. 其它 additional packets

本地 session state 通过 `additional_packets` 传入，但 `metadata.source == "local_session_state"` 的 packet 会被提前到 Markdown/full 记忆之前。这保证当前 active 会话的任务状态优先于长期记忆。

同时保留了旧构造兼容：

```python
ContextBuilder(memory_tool, rag_tool, ContextConfig(...))
```

第三个位置参数如果是 `ContextConfig`，会自动挪回 `config`，避免新增 `md_memory_provider` 后老调用错位。

## 6. System Prompt 写入指引

light 模式不注册新的记忆工具。模型要保存记忆时，仍然使用现有 `file_read` / `file_write`。

`run_agent.py` 会把轻量记忆说明追加进 system prompt，内容包括：

- 用户全局记忆目录。
- 当前项目记忆目录。
- `MEMORY.md` 是索引文件。
- 记忆文件建议的 frontmatter。
- 用户明确要求“记住/保存偏好/保存项目事实”时，先读后写。
- 长期偏好写 global，项目事实写 project。
- 不要凭空写入记忆。

这样不需要新增 `memory` 工具，也能让 agent 在用户明确要求时维护 Markdown 记忆。

## 7. 依赖瘦身

`pyproject.toml` 与 requirements 被拆成两档：

- `pip install -e .` 或 `pip install -r requirements.txt`：轻量 core 依赖。
- `pip install -e ".[full]"` 或 `pip install -r requirements-full.txt`：旧完整能力依赖。

`langdetect` 已从 core 依赖移到 full，因为它只在 RAG pipeline 中使用。向量库、embedding、多模态、PDF、外部搜索相关依赖也集中在 full extras。

## 8. 测试覆盖

新增/更新的关键验证点：

- `ContextBuilder` 能注入 project + global Markdown 记忆内容。
- `context.builder` 子进程导入后，`sys.modules` 中没有 `tools.tools.memory_tool` / `tools.tools.rag_tool`。
- TUI transport 默认 spawn 参数包含 `--memory-system light`。
- 旧 memory/RAG mock 测试仍然通过，证明 full 路径未被删除。
- `py_compile`、Python 测试、TUI `npm test` 和 `npm run build` 用于验证跨语言改动。

## 9. 取舍

轻量 Markdown 记忆牺牲了向量检索的语义召回能力，但换来：

- 默认安装更轻。
- 新用户无需配置向量数据库 API 或 embedding 模型。
- 记忆文件可直接审计、编辑、复制和版本化。
- 旧 full 能力仍可通过显式参数启用。

后续如果要增强轻量召回，可以优先考虑纯本地的关键词索引、标题权重、路径权重和 frontmatter 字段权重，而不是重新引入 embedding 或外部向量服务。
