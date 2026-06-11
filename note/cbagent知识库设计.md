# cb-agent 知识库设计

## 1. 目录结构

cb-agent 的结构化知识库默认位于：

```text
~/knowledge/
├── README.md
├── index.json
├── graph.json
└── pages/
    └── <topic>.md
```

可通过 `CBAGENT_KNOWLEDGE_DIR` 覆盖。`pages/` 中的 Markdown 页面是源数据，`index.json` 和 `graph.json` 是可重建的稳定接口，面向未来 Web 文档浏览和知识图谱视图。

## 2. 页面格式

知识页面使用简单 frontmatter：

```markdown
---
id: memory-architecture-1234abcd
title: Memory Architecture
created_at: 2026-06-11T10:30:00+08:00
updated_at: 2026-06-11T10:30:00+08:00
tags: ["memory", "architecture"]
source: conversation
namespace: workspace:xxxxxxxxxxxx
---

# Memory Architecture

## Source question

...

## Consolidated answer

...

## Related

- [[RAG Context]]
```

`[[Page Title]]` 是页面之间的交叉引用语法。刷新索引时，知识库会把能匹配到现有页面标题的引用转换为 `graph.json` 中的 `references` 边。

## 3. 稳定接口

`KnowledgeBase` 暴露这些接口：

| 接口 | 用途 |
|---|---|
| `ensure_structure()` | 创建 `README.md`、`index.json`、`graph.json`、`pages/` |
| `upsert_page(title, body, tags, source, metadata)` | 新建或追加更新知识页 |
| `refresh_indexes()` | 从 Markdown 页面重建索引和图数据 |
| `read_index()` | 返回文档浏览 UI 可用的页面索引 |
| `read_graph()` | 返回知识图谱 UI 可用的节点和边 |
| `render_related_context(query)` | 按当前用户问题检索相关页面片段，注入模型上下文 |
| `capture_turn(user_text, assistant_text, work_record_text, long_term_memory_path)` | 每轮对话后的自动捕获入口 |

`index.json` 示例：

```json
{
  "version": 1,
  "updated_at": "...",
  "pages": [
    {
      "id": "memory-architecture-1234abcd",
      "title": "Memory Architecture",
      "path": "C:/Users/.../knowledge/pages/memory-architecture-1234abcd.md",
      "tags": ["memory", "architecture"],
      "summary": "..."
    }
  ]
}
```

`graph.json` 示例：

```json
{
  "version": 1,
  "updated_at": "...",
  "nodes": [
    {"id": "memory-architecture-1234abcd", "label": "Memory Architecture"}
  ],
  "edges": [
    {"source": "rag-context-5678abcd", "target": "memory-architecture-1234abcd", "type": "references"}
  ]
}
```

## 4. 每轮检索与上下文注入

每次向模型发起请求时：

```text
AgentSession._build_chat_messages(memory_query=user_query)
  -> get_system_prompt(..., memory_query)
    -> memory_section(memory_loader, query=memory_query)
      -> MemoryLoader.get_knowledge_context(query)
        -> KnowledgeBase.render_related_context(query)
```

`render_related_context()` 先执行 Markdown lexical search，再在可用时尝试 vector/RAG search。返回内容会被 `format_memory_files()` 放进 memory section 的 `Retrieved knowledge context` 段。

## 5. 自动捕获与更新

对话结束后：

```text
AgentSession._auto_update_memory_and_knowledge()
  -> MemoryLoader.record_turn()
    -> KnowledgeBase.capture_turn()
      -> append_long_term_memory()
      -> upsert_page()
      -> refresh_indexes()
      -> optional RAG indexing
```

写入策略：

- 用户明确表达“记住、偏好、以后、重要、必须、不要忘”等长期信号时，追加一条去重后的 bullet 到 `~/MEMORY.md`。
- 对架构、设计、实现、接口、方案、决策、规则、流程等可复用知识，生成或追加 Markdown 页面。
- 页面写入后立即重建 `index.json` 和 `graph.json`。
- 模型可通过显式工具 `knowledge_write` / `knowledge_search` 主动写入和检索知识；自动捕获仍作为 best-effort 后台补充。
- 默认只使用 Markdown 页面与关键词检索；只有设置 `CBAGENT_ENABLE_FULL_MEMORY=1` 时，RAG pipeline / 向量库 / embedding 才会 best-effort 启用。

## 6. Web UI 与知识图谱预留

未来 Web 页面可以只依赖稳定文件接口：

- 文档浏览：读取 `index.json`，按 `path` 打开 Markdown 页面。
- 标签筛选：使用 `pages[].tags`。
- 图谱视图：读取 `graph.json` 的 `nodes` 和 `edges`。
- 页面详情：读取 `pages/*.md`，解析 frontmatter 和正文。

这使前端不必耦合 Python 内部类，也不用直接扫描目录。Python 侧只负责维护源页面和两个派生索引。

## 7. 存储原则

- Markdown 是源数据，JSON 是索引。
- 结构化知识放 `pages/`，短句偏好放 `~/MEMORY.md`。
- 自动捕获只做保守更新，不阻塞对话主流程。
- RAG 是增强层，不是唯一事实源。
- 索引可以随时删除并由 `refresh_indexes()` 重建。

相关实现入口：

- `context/memory/knowledge.py`
- `context/memory/loader.py`
- `context/sections/dynamic_sections.py`
- `agent/session.py`
