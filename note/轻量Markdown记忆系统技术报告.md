# Markdown 记忆与指令预算技术报告

> 更新于 2026-07-26。对应 `context/memory/*`、`context/sections/dynamic_sections.py`。

## 1. 分层文件（实际加载顺序）

`MemoryLoader` 按类型加载（数组靠后在 prompt 中仍按原加载顺序输出；**预算优先级**见下节）：

1. **Managed** — 系统/组织级 CLAUDE.md + rules  
2. **User / Global** — `~/.cbagent` 与用户核心 MEMORY/AGENT 等  
3. **Project** — 从仓库根到 cwd 的 CLAUDE.md / `.cbagent` 等（更靠近 cwd 的条目在同类型内优先占预算）  
4. **ShortTerm / Local** — `.cbagent/SHORT_TERM.md`、`CLAUDE.local.md`  

结构化知识（RAG/pages）走独立 `knowledge` section（**request-only**），与长期 instructions 拆开，避免 query 变化导致整份 AGENT/CLAUDE 再写入 durable history。

## 2. 注入通道

| 内容 | section 名 | persistence |
|------|------------|-------------|
| 格式化后的 memory 文件 | `instructions` | persistent（进 world state diff） |
| 本轮检索知识 | `knowledge` | request_only |

每轮构建前 `reset_cache(reason=memory_sections_realtime_reload)`，保证文件修改尽快可见。  
读取失败 → 抛出 `MemoryReadError`，由 dynamic section **error** 语义处理（不发 removed）。文件明明存在却打不开时，禁止返回空内容并伪装 absent。
`MemoryBudgetError`（Managed 装不下）→ **阻止本轮请求**，禁止半截 Managed。

## 3. 严格预算 `enforce_memory_budget`

- 默认总预算：`MAX_MEMORY_CHARACTER_COUNT = 40_000` **字符**（含 formatter 标题与 `MEMORY_INSTRUCTION_PROMPT` 开销）。
- 优先级（高 → 低）：Managed > User/Global > Project（近 cwd 优先）> ShortTerm/Local。
- **Managed** 必须完整纳入；超预算或超单文件策略 → `MemoryBudgetError`。
- **User/Global/Project** 装不下 → **整文件 omitted**（指令不允许无提示截断）。
- **ShortTerm/Local** 可注入带路径来源的 **preview**，并记入 truncated 列表。
- 模型可见 **Memory budget manifest** 列出 omitted / truncated 路径。

单文件：

- `MAX_FILE_BYTES`（256KB）约束“是否完整进 prompt”的策略，**不再**在 `@include` 解析前静默截断。
- include 扫描硬上限 `MAX_INCLUDE_SCAN_BYTES`（2MB）；超过则抛 `MemoryReadError`，避免尾部 include 静默消失。

首轮没有可沿用的 `instructions` baseline 时，`MemoryReadError` 会一路阻止模型请求；已有 baseline 时保留旧值并记录 error，下一次成功读取后再产生正常 diff。

## 4. Formatter

`format_memory_files(files, omitted=..., truncated_paths=...)`：

```text
MEMORY_INSTRUCTION_PROMPT
Contents of <path> (<type label>):

<body>

Memory budget manifest (files not fully injected):
- omitted (Local): ...
- truncated preview: ...
```

## 5. 与旧「轻量/full 模式」文档的关系

历史文档中的 `--memory-system light|full` 与 `context/markdown_memory.py` 描述可能已迁移；**当前主路径**以 `MemoryLoader` + 用户/项目 Markdown 指令文件 + 可选 knowledge base 为准。启用向量/RAG 工具仍由运行装配决定，不改变 instructions 预算规则。

## 6. 测试

- `test/test_memory_loader.py`：include 循环/深度、预算不挤掉 Managed、formatter 开销、Managed 过大失败、256KB 后 include 仍解析。

## 7. 关键代码

- `context/memory/loader.py` — 加载、include、`enforce_memory_budget`  
- `context/memory/formatter.py` — 注入文本与 manifest  
- `context/memory/paths.py` — 路径发现  
- `context/sections/dynamic_sections.py` — `memory_sections`  
