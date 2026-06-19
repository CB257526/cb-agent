# cb-agent 上下文构建与跨会话持久记忆构建

## 1. 目标

本设计把 cb-agent 的上下文构建拆成稳定、可维护的几个层次，并把记忆系统整理成清晰的三层加载链：

| 层级 | 默认位置 | 进入上下文的位置 | 适合内容 |
|---|---|---|---|
| 全局记忆 | `~/AGENT.md`、`~/USER.md`、`~/RULE.md`、`~/MEMORY.md` | 每轮 system prompt 的 memory dynamic section | 用户身份、长期偏好、全局规则、跨项目事实 |
| 项目记忆 | 当前项目链上的 `AGENT.md`、`USER.md`、`RULE.md`、`MEMORY.md`、`.cbagent/*.md`、旧 `CLAUDE.md` | 全局记忆之后 | 仓库约定、项目事实、当前代码库维护说明 |
| 短期记忆 | 当前项目 `.cbagent/SHORT_TERM.md` | 项目记忆之后 | 当前任务进展、临时决策、近期上下文 |

`~/.cbagent/CLAUDE.md`、`~/.cbagent/*.md`、`.claude/CLAUDE.md`、`.cbagent/CLAUDE.md` 等旧路径仍兼容加载，但新文档和默认模板以 `~/AGENT.md` 等根工作区文件为准。

## 2. 每轮上下文构建流程

核心调用链：

```text
AgentSession.chat(user_query)
  -> _build_chat_messages(memory_query=user_query)
    -> get_static_system_prompt(...)
      -> first stable system message
    -> append persisted history
    -> get_dynamic_context_prompt(..., memory_query)
      -> context_update user message
        -> memory_section(memory_loader, query=memory_query)
          -> MemoryLoader.get_memory_files()
          -> MemoryLoader.get_knowledge_context(query)
          -> format_memory_files(files, knowledge_context)
    -> append current user query
```

`memory_section` 是 uncached section，每轮都会重读记忆文件和相关知识库片段。这样用户在上一轮通过工具或自动捕获写入的内容，下一轮就能进入模型上下文。

## 3. 三层记忆加载顺序

`MemoryLoader._compute_memory_files()` 的有效顺序是：

1. Managed legacy memory：管理员级旧路径，通常为空。
2. User legacy memory：`~/.cbagent/CLAUDE.md`、`~/.cbagent/rules/*.md`、`~/.cbagent/{AGENT,USER,RULE,MEMORY}.md`。
3. Global workspace memory：`~/AGENT.md`、`~/USER.md`、`~/RULE.md`、`~/MEMORY.md`。
4. Project memory：从文件系统根目录到当前 `cwd` 逐层扫描项目记忆文件。
5. Short-term memory：当前项目 `.cbagent/SHORT_TERM.md`。
6. Local legacy memory：当前项目 `CLAUDE.local.md`。

越靠后的内容在 prompt 中越靠后，优先级也越高。全局、项目、短期三层是日常维护的主路径，旧路径只作为迁移兼容。

## 4. 跨会话持久记忆写入

每轮对话完成后，`AgentSession._auto_update_memory_and_knowledge()` 会做一次 best-effort 捕获：

```text
AgentSession._auto_update_memory_and_knowledge()
  -> MemoryLoader.record_turn()
    -> KnowledgeBase.capture_turn()
      -> append_long_term_memory(~/MEMORY.md)
      -> upsert_page(~/knowledge/pages/*.md)
      -> refresh_indexes()
```

捕获策略保持保守：

| 目标 | 触发条件 | 写入位置 |
|---|---|---|
| 长期记忆 | 用户表达“记住、以后、偏好、重要、必须、不要忘”等信号 | `~/MEMORY.md` |
| 结构化知识 | 对话包含架构、设计、实现、接口、知识、方案、决策、规则、流程、重构等信号，或工作记录足够长 | `~/knowledge/pages/*.md` |

自动捕获失败不会中断主对话，只写 debug/error 日志。写入长期记忆后会 reset memory cache，确保下一轮重新加载。

## 5. light 与 full 的边界

light 模式是默认路径：

- Markdown 文件是长期记忆的轻量事实源。
- 知识库页面也是 Markdown 文件，`index.json` 和 `graph.json` 是派生索引。
- 不注册旧 `memory` / `rag` 工具，不要求 embedding、向量库或图数据库。

full 模式保留旧的 `MemoryTool` / `RAGTool`：

- 通过 `python run_agent.py --memory-system full` 显式启用。
- 适合需要旧 episodic / semantic / working 记忆工具和完整 RAG pipeline 的场景。
- 不取代轻量三层 Markdown 记忆架构。

知识库可以在 light 模式下尝试接入 vector/RAG 后端，但这是可选的 best-effort 增强。Markdown 页面仍是源数据。

## 6. 启动初始化

`run_agent.py` 的 light 模式会保证这些基础文件和目录存在：

```text
~/
├── AGENT.md
├── USER.md
├── RULE.md
├── MEMORY.md
└── knowledge/
    └── pages/

<project>/
└── .cbagent/
    └── SHORT_TERM.md
```

全局工作区可用 `CBAGENT_WORKSPACE_DIR` 覆盖。知识库根目录可用 `CBAGENT_KNOWLEDGE_DIR` 单独覆盖。

## 7. 维护约定

- 人格和长期行为写入 `AGENT.md`。
- 用户身份、稳定偏好写入 `USER.md`。
- 自定义约束和不可违背规则写入 `RULE.md`。
- 可追加的事实型长期记忆写入 `MEMORY.md`。
- 当前任务状态写入 `.cbagent/SHORT_TERM.md`，完成后可清理或沉淀到项目 `MEMORY.md`。
- 可复用、可引用、适合未来浏览的内容写成知识库页面。
- 大段资料不要直接塞进 `MEMORY.md`，应写入 `~/knowledge/pages/`，通过检索按需进入上下文。

相关实现入口：

- `context/memory/paths.py`
- `context/memory/loader.py`
- `context/memory/formatter.py`
- `context/sections/dynamic_sections.py`
- `context/prompts/builder.py`
- `agent/session.py`
- `context/memory/knowledge.py`
