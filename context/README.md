# 上下文工程模块（context）

本模块为 cb-agent 框架提供**上下文构建（Context Engineering）**能力。
核心入口是 [`ContextBuilder`](builder.py)，遵循 **GSSC** 流水线：
Gather（收集）→ Select（筛选）→ Structure（组织）→ Compress（压缩）。

---

## 目录

- [一、为什么需要上下文构建](#一为什么需要上下文构建)
- [二、整体设计](#二整体设计)
- [三、快速上手](#三快速上手)
- [四、核心数据结构](#四核心数据结构)
- [五、`ContextBuilder` 方法详解](#五contextbuilder-方法详解)
- [六、配置项一览（`ContextConfig`）](#六配置项一览contextconfig)
- [七、性能与正确性要点](#七性能与正确性要点)
- [八、扩展指引](#八扩展指引)

---

## 一、为什么需要上下文构建

LLM 调用之前需要把多源信息拼装成一段提示词。简单 `messages=[...]` 拼接在小场景能跑，
但一旦同时存在以下任一需求，就需要专门的上下文工程层：

- 长对话历史 + 本地会话状态 + Markdown 记忆 + 可选 full 记忆/RAG + 系统指令同时存在，需要按预算裁剪
- 不同信息有**不同优先级**（系统指令 > 任务态 > 证据 > 历史）
- 候选片段需要**相关性筛选**，避免把无关 RAG/记忆塞进 prompt
- 多条相似证据需要**去冗余**（MMR），节省 token
- 超预算时不能简单按行截断，必须**保结构**（不能切断节标题）

`ContextBuilder` 把这些关注点拆成可独立测试、可独立替换的阶段。

---

## 二、整体设计

### 2.1 GSSC 流水线

```
原始输入                                        最终 prompt
────────                                        ──────────
user_query        ┌─────────────┐ packets ┌─────────────┐ packets ┌─────────────┐  text  ┌─────────────┐
system_instr ───▶ │   Gather    │────────▶│   Select    │────────▶│  Structure  │───────▶│  Compress   │───▶ ctx
history           │             │         │             │         │             │        │             │
additional        │ state/md    │         │ relevance + │         │ 按优先级    │        │ 整段丢弃    │
                  │ memory/rag  │         │ recency +   │         │ 拼接成节    │        │ 保结构      │
                  │ → packets   │         │ MMR + 预算  │         │             │        │             │
                  └─────────────┘         └─────────────┘         └─────────────┘        └─────────────┘
```

每一步都是纯函数式（输入 → 输出），中间状态是 `List[ContextPacket]`。

### 2.2 优先级即结构

四个优先级**同时**决定两件事：

| 优先级 | 含义 | 进入哪一节 | 是否可被丢弃 |
|---|---|---|---|
| `P0_SYSTEM` | 系统指令 / 角色策略 | `[Role & Policies]` | 永不丢弃 |
| `P1_STATE` | 任务态、关键进展、未决问题 | `[State]` | 超预算时最后丢 |
| `P2_EVIDENCE` | 记忆/RAG/外部证据 | `[Evidence]` | 按 min_relevance 过滤、按预算丢 |
| `P3_HISTORY` | 对话历史 | `[Context]` | 超预算时最先丢 |

固定节模板：

```
[Role & Policies]
{system_instructions}

[Task]
用户问题：{user_query}

[State]
关键进展与未决问题：
{P1 packets}

[Evidence]
事实与引用：
{P2 packets}

[Context]
对话历史与背景：
{P3 packets}

[Output]
请按以下格式回答：...
```

### 2.3 同步 / 异步双入口

- `build()` / `build_detailed()`：同步串行扫描 Markdown 记忆，并在 full 模式下调用旧 memory、RAG
- `abuild()` / `abuild_detailed()`：内部用 `asyncio.gather + asyncio.to_thread`
  把 Markdown 记忆、任务态记忆、相关记忆、RAG **并发**触发

工具本身是同步代码，但通过 `to_thread` 让 IO 重叠，
通常能把端到端检索延迟从 `t1+t2+t3` 降到 `max(t1,t2,t3)`。

---

## 三、快速上手

### 3.1 最小例子

```python
from context import ContextBuilder, ContextConfig
from core.message import Message

builder = ContextBuilder(config=ContextConfig(max_tokens=4000))

ctx = builder.build(
    user_query="数据库连接超时怎么办",
    system_instructions="你是资深 DBA",
    conversation_history=[
        Message.create_user_message("我们项目用的 PostgreSQL"),
        Message.create_assistant_message("好的，记下了"),
    ],
)
print(ctx)
# 直接拿来作为 LLM 的单条 system/user prompt
```

### 3.2 接入轻量 Markdown 记忆（默认模式）

轻量模式不需要 embedding、向量库或任何 RAG env，只扫描全局/项目两级 Markdown 文件：

```python
from context import ContextBuilder, ContextConfig, MarkdownMemoryProvider

provider = MarkdownMemoryProvider(project_dir=".")
provider.ensure_initialized()  # 创建 ~/.cbagent/memory 和 ./.cbagent/memory 的 MEMORY.md 模板

builder = ContextBuilder(
    md_memory_provider=provider,
    config=ContextConfig(max_tokens=4000),
)
ctx = builder.build(user_query="按我的项目约定继续处理")
```

目录约定：

| 级别 | 默认路径 | 说明 |
|---|---|---|
| global | `~/.cbagent/memory/` | 用户长期偏好、跨项目事实 |
| project | `<project_root>/.cbagent/memory/` | 当前仓库的项目事实、约定、进展 |

每个目录都有一个 `MEMORY.md` 索引，具体记忆文件是同目录下其它 `.md` 文件。记忆文件建议带最小 frontmatter：

```markdown
---
name: 用户偏好
description: 用户希望回答保持中文并附验证命令
type: user
scope: global
---

用户偏好：回答使用中文，必要时列出验证命令。
```

### 3.3 接入 full memory 与 RAG（可选完整模式）

```python
from tools.tools.memory_tool import MemoryTool
from tools.tools.rag_tool import RAGTool
from context import ContextBuilder, ContextConfig

# MemoryTool / RAGTool 只应在 --memory-system full 下启用。
builder = ContextBuilder(
    memory_tool=MemoryTool(),
    rag_tool=RAGTool(),
    config=ContextConfig(max_tokens=8000, min_relevance=0.05),
)
ctx = builder.build(user_query="...", conversation_history=[...])
```

记忆与 RAG 的检索查询、top_k、重要度阈值都通过 `ContextConfig` 暴露，
不需要继承或改动 `ContextBuilder`。

### 3.4 异步并发触发

```python
ctx = await builder.abuild(
    user_query="数据库连接超时怎么办",
    conversation_history=history,
)
```

接口签名与 `build()` 完全一致，仅内部并发策略不同。

### 3.5 注入额外证据（`additional_packets`）

```python
from context import ContextPacket, ContextPriority

extra = ContextPacket(
    content="PostgreSQL 默认 statement_timeout=0（不限制）",
    priority=ContextPriority.P2_EVIDENCE,
    metadata={"source": "manual"},
)
ctx = builder.build(user_query="...", additional_packets=[extra])
```

`additional_packets` 默认在 Gather 阶段末尾追加，与其它来源的 packet 一视同仁
（同样要过 min_relevance、MMR、预算）。

一个特例是 `metadata={"source": "local_session_state"}` 的 packet：它来自
`LocalSessionStore.state.json`，代表当前 active 会话的滚动工作状态，会被提前到
system 指令之后、Markdown/full 记忆之前，避免当前任务状态被长期记忆淹没。

### 3.6 直接拿到 OpenAI messages：`to_messages` / `ato_messages`

`build()` 返回的是一段拼好的 prompt 字符串。OpenAI 协议要的是
`messages=[{"role": ..., "content": ...}, ...]` 列表，
所以多数场景下你需要的是 `to_messages`：

```python
from agent.cb_agents import CbAgentsLLM

builder = ContextBuilder(config=ContextConfig(max_tokens=4000))

messages = builder.to_messages(
    user_query="数据库连接超时怎么办",
    system_instructions="你是资深 DBA",
    conversation_history=[
        Message.create_user_message("我们项目用的 PostgreSQL"),
        Message.create_assistant_message("好的，记下了"),
    ],
)
# messages 形如：
# [
#   {"role": "system",  "content": "<完整的 GSSC 拼装结果>"},
#   {"role": "user",    "content": "数据库连接超时怎么办"},
# ]

llm = CbAgentsLLM()
result = llm.think(messages)
print(result["answer"])
```

异步版：`messages = await builder.ato_messages(user_query=..., ...)`，
内部走 `abuild`，memory/rag 检索并发触发。

> 约定：`user_query` 在 system 的 `[Task]` 节出现一次，user message 里再出现一次。
> 这是冗余但符合 OpenAI 风格的写法 —— 多数厂商对"最后一条 user 消息为当前问题"
> 的格式表现更稳。如果你只想要一条 message，直接用 `build()` 拿字符串塞进
> `[{"role": "user", "content": ctx}]` 即可。

### 3.7 调试：`build_detailed` / `abuild_detailed`

需要看是哪些片段被丢弃、是否触发了截断时使用：

```python
result = builder.build_detailed(user_query="...", ...)
print(result.context)        # 最终 prompt
print(result.selected)       # 选中的 packet 列表
print(result.dropped)        # [(packet, 原因), ...]
print(result.truncated)      # 是否触发了 _compress 整段丢弃
print(result.total_tokens)   # 最终 token 数
```

---

## 四、核心数据结构

### 4.1 `ContextPriority(IntEnum)`

四档优先级。用 `IntEnum` 而非字符串，避免 `metadata["type"] == "system"` 这种字面量硬匹配。

### 4.2 `ContextPacket`

```python
@dataclass
class ContextPacket:
    content: str                                          # 文本内容
    priority: ContextPriority = ContextPriority.P2_EVIDENCE
    timestamp: datetime = field(default_factory=_now_utc) # UTC 时间戳，用于新近性
    metadata: Dict[str, Any] = field(default_factory=dict)
    relevance_score: float = 0.0                          # _select 计算后回填
    _token_count: Optional[int] = None                    # 懒计算缓存
```

**关键设计**：`token_count` 是 `@property`，仅在第一次访问时调 `count_tokens`。
原版本在 `__post_init__` 强制 token 化，批量构造 packet 时会反复触发编码器。
现在懒计算 + lru_cache 后，构造 1000 个 packet 几乎零开销。

### 4.3 `ContextConfig`

见第六节。覆盖了预算、相关性、MMR、记忆/RAG 检索等所有可调参数。

### 4.4 `ContextResult`

```python
@dataclass
class ContextResult:
    context: str                                  # 最终 prompt
    selected: List[ContextPacket]
    dropped: List[Tuple[ContextPacket, str]]      # (被丢的 packet, 原因)
    truncated: bool                               # 是否进入了 _compress 整段丢弃
    total_tokens: int
```

---

## 五、`ContextBuilder` 方法详解

下面按调用顺序讲解。私有方法（`_` 开头）也都暴露出来便于自定义子类替换。

### 5.1 公开入口

#### `build(user_query, conversation_history=None, system_instructions=None, additional_packets=None) -> str`

同步构建。内部走 `_gather_sync → _finalize`，返回最终 prompt 字符串。

#### `abuild(...) -> str`

异步版本。`_gather_async` 用 `asyncio.gather + asyncio.to_thread`
让 Markdown 记忆 / memory_state / memory_related / rag 四路检索并发触发。

#### `build_detailed(...) -> ContextResult` / `abuild_detailed(...) -> ContextResult`

返回完整 `ContextResult`，包含被丢弃片段及原因。生产环境若开 debug 日志可记录这个对象。

#### `to_messages(...) -> List[Dict[str, str]]` / `ato_messages(...) -> List[Dict[str, str]]`

OpenAI messages 协议适配。返回 `[{role: system, content: <ctx>}, {role: user, content: <user_query>}]`，
直接喂给 `CbAgentsLLM.think(messages)` 即可。`ato_messages` 是异步版（走 `abuild`）。

### 5.2 Gather 阶段

#### `_gather_sync(user_query, conversation_history, system_instructions, additional_packets) -> List[ContextPacket]`

按固定顺序拼装 packet：
1. system_instructions → P0
2. `additional_packets` 中的 `local_session_state` → P1（当前会话滚动状态）
3. Markdown memory state（轻量记忆状态）→ P1
4. Markdown memory related（轻量相关记忆）→ P2
5. full memory_state（任务态）→ P1
6. full memory_related（与查询相关）→ P2
7. full rag（知识库）→ P2
8. conversation_history → P3
9. 其它 additional_packets → 按 packet 自身 priority

#### `_gather_async(...) -> List[ContextPacket]`

同上，但 Markdown memory / full memory / full rag 检索用 `asyncio.gather` 并发。

#### `_make_system_packet(instructions)` / `_make_history_packet(history)`

把字符串 / Message 列表转成 packet。`_make_history_packet` 内部用
`messages_to_text` 处理 `Message`，自动处理多模态 content 与 `MessageRole` 枚举。

#### `_search_markdown_memory(user_query) -> Tuple[Optional[ContextPacket], Optional[ContextPacket]]`

扫描 `MarkdownMemoryProvider` 提供的项目级/全局 Markdown 文件。状态类记忆进入
`[State]`，与当前 query 相关的片段进入 `[Evidence]`。这个路径不 import 旧
`tools.tools.memory_tool` / `tools.tools.rag_tool`，因此轻量安装不会触碰向量/RAG 依赖。

#### `_search_memory_state() -> Optional[ContextPacket]`

遍历 `config.memory_state_types`（默认 `("working", "episodic", "semantic")`），
分别搜索任务态记忆并合并。只在调用方传入旧 `memory_tool` 时生效，通常对应
`--memory-system full`。

#### `_search_memory_related(user_query) -> Optional[ContextPacket]`

按用户查询召回相关记忆（不限 memory_type）。

#### `_search_rag(user_query) -> Optional[ContextPacket]`

调用 `rag_tool.run({"action": "search", "query": ..., "top_k": ...})`。只在调用方传入
旧 `rag_tool` 时生效，通常对应 `--memory-system full`。

#### `_normalize_tool_output(raw) -> str`（静态方法）

统一处理工具返回：`None` → 空串，其它 `str(raw).strip()`。
**不再做 `"未找到" not in result` 这种字面匹配**——工具改提示语就坏；
让工具自己负责返回明确的空结果即可。

### 5.3 Select 阶段

#### `_select(packets, user_query) -> Tuple[List[selected], List[(dropped, reason)]]`

整个流水线最复杂的一步。流程：

1. **算分**：每个 packet 计算
   - `relevance_score`：`jaccard(tokenize(query), tokenize(content))`
   - `recency`：`exp(-Δt / tau)` 指数衰减
   - 综合分 `_score = w_rel * relevance + w_rec * recency` 缓存到 metadata
2. **分桶**：
   - P0 → 强制纳入（不算分、不过滤）
   - P1 / P3 → forced（按预算填，超预算时仍纳入）
   - P2 → evidence（按预算严格过滤）
3. **过滤**：P2 按 `min_relevance` 阈值过滤
4. **重排 P2**：
   - 若 `enable_mmr=True` → 调 `_mmr_rerank`
   - 否则按综合分降序
5. **预算填充**：P0 → forced(P1 → P3) → P2 evidence，每加一个就累加 `token_count`
6. **超预算处理**：
   - P1 / P3 即使超预算也保留（_compress 兜底）
   - P2 严格遵守预算，超出则丢

被丢的 packet 都会进 `dropped` 列表，附带原因字符串。

#### `_recency_score(ts, now) -> float`

指数衰减。`tau = config.recency_tau_seconds`（默认 3600s）：
1 小时前 ~ 0.37，2 小时前 ~ 0.13。可调小让历史快速衰减、调大让历史更"长寿"。

兼容 naive datetime（无时区的旧 packet 会被默认当 UTC 处理）。

#### `_mmr_rerank(packets, query_tokens, mmr_lambda) -> List[ContextPacket]`

经典 **Maximal Marginal Relevance** 贪心重排：

```
score(c) = λ * rel(c, query) - (1-λ) * max_{s ∈ selected} sim(c, s)
```

- `rel` 用 query 与 packet 的 jaccard（**纯相关性**，不用 `_score` 综合分，
  否则新近性会污染"相关 vs 多样"的权衡）
- `sim` 用 packet 间的 jaccard
- `λ=1` 退化为纯相关排序，`λ=0` 退化为纯多样性
- 默认 `λ=0.7`：偏相关，但会降低重复证据的权重

预先把所有候选的 token 集合 cache 进 `token_map`，避免重复 tokenize。

### 5.4 Structure 阶段

#### `_structure(selected, user_query) -> str`

按优先级聚合到固定 6 节模板。关键细节：

- 用 `priority` 字段（`IntEnum`）筛选，**不再用 `metadata["type"]` 字符串匹配**
- `[Output]` 用 `textwrap.dedent(...)` 处理多行字符串缩进，避免 12 空格直接进 prompt
- 各节用 `\n\n` 分隔；空节直接省略（除 `[Task]` 与 `[Output]` 始终存在）

### 5.5 Compress 阶段

#### `_compress(context) -> Tuple[str, bool]`

整段丢弃式压缩，**不按行切**（按行会切断节标题，破坏结构）。

1. 若 `count_tokens(context) <= available`，直接返回，`truncated=False`
2. 调 `_split_sections` 把已生成文本按已知节标题切回 OrderedDict
3. 按可丢顺序逐节剥离：`[Context] → [Evidence] → [State]`
4. 每丢一节就重算 token，达标即返回 `truncated=True`
5. 全部可丢节都剥光仍超预算 → 极端兜底：用 tiktoken `encode/decode` 硬截断

`[Role & Policies]` / `[Task]` / `[Output]` 始终保留，保证 prompt 仍是可执行的最小完整结构。

#### `_split_sections(context) -> Dict[str, str]`（类方法）

用 `_SECTION_HEADERS` 白名单匹配，避免内容里偶然出现的 `[xxx]` 被误判成节标题。
返回 OrderedDict，节顺序保持 _structure 生成时的顺序。

### 5.6 模块级 helper

| 函数 | 职责 |
|---|---|
| `_get_encoding()` | `@lru_cache(maxsize=1)` 包裹的 tiktoken 编码器，进程内仅初始化一次 |
| `count_tokens(text, model_name=None)` | 用全局 encoder 计 token；空串返 0；异常时降级 `len(text)//4` |
| `tokenize_for_relevance(text)` | `@lru_cache(maxsize=512)` 文本 → frozenset[int]，相关性专用 |
| `jaccard(a, b)` | 标准 Jaccard；空集合 → 0 |
| `_extract_message_text(msg)` | 单条 Message → 文本，处理 `MessageRole.value` 与多模态 content |
| `messages_to_text(messages, max_messages)` | 多条 Message → 多行文本 |

这些 helper 都放在 [`utils/common.py`](../utils/common.py)（叶子模块）作为唯一定义来源，
`context/builder.py` 反向 import，避免循环依赖。

---

## 六、配置项一览（`ContextConfig`）

| 字段 | 默认 | 含义 |
|---|---|---|
| **预算** | | |
| `max_tokens` | 8000 | 总预算（含生成余量） |
| `reserve_ratio` | 0.15 | 给生成留出的余量比例（实际可用 = max * (1-ratio)） |
| **筛选** | | |
| `min_relevance` | 0.05 | P2 证据的相关性下限。token 集合 Jaccard 中文短查询交集天然较小，所以阈值低 |
| `enable_mmr` | True | 是否对 P2 启用 MMR 重排 |
| `mmr_lambda` | 0.7 | MMR 权衡系数：1=纯相关，0=纯多样 |
| **评分权重** | | |
| `relevance_weight` | 0.7 | 综合分中相关性的权重 |
| `recency_weight` | 0.3 | 综合分中新近性的权重 |
| `recency_tau_seconds` | 3600 | 新近性指数衰减时间尺度（秒） |
| **压缩** | | |
| `enable_compression` | True | 是否启用 _compress 阶段 |
| **记忆** | | |
| `md_memory_provider` | 构造参数 | 轻量 Markdown 记忆 provider，不在 `ContextConfig` 内 |
| `memory_state_query` | `"任务状态 子目标 结论 阻塞"` | 任务态记忆查询。**用空格分隔**，对向量检索友好（原版本 `"A OR B OR C"` 对向量检索无意义） |
| `memory_state_min_importance` | 0.7 | 任务态记忆最小重要度 |
| `memory_state_limit` | 5 | 单类型任务态记忆 top_k |
| `memory_state_types` | `("working", "episodic", "semantic")` | 任务态搜哪几类记忆 |
| `memory_related_limit` | 5 | 相关记忆 top_k |
| **RAG** | | |
| `rag_top_k` | 5 | RAG 检索 top_k |
| **历史** | | |
| `history_max_messages` | 10 | 转入 prompt 的最近消息条数 |

---

## 七、性能与正确性要点

### 7.1 性能

- **tiktoken 编码器单例**：`_get_encoding` 用 `lru_cache(1)` 包裹，进程内仅初始化一次。
  原版本每次 `count_tokens` 都重新加载，冷启 50–100ms × N 次直接拖慢整个流程
- **token 集合 lru_cache**：`tokenize_for_relevance` cache 512 项，热路径 query 命中率高
- **packet token 懒计算**：`ContextPacket.token_count` 仅在 select 阶段第一次访问时算
- **并发检索**：`abuild` 用 `to_thread + gather` 把三路 IO 重叠

测得：1000 次 `count_tokens` 从原 50–100ms × 1000 降到 3.2 μs / 次，量级约 15000–30000×。

### 7.2 正确性

- **Message 多模态**：`_extract_message_text` 正确处理 `content: List[Dict]` 的多模态结构，
  抽出 text/image_url/audio_url；不再把整个 dict `str()` 化进 prompt
- **MessageRole 枚举**：用 `.value` 而非 `str(MessageRole.USER)`，避免出现 `[MessageRole.USER]`
- **中文相关性**：用 token id 集合 + Jaccard，不依赖 `split()`/jieba。
  原版本 `query.lower().split()` 对无空格中文整句只产 1 个 token，相关性几乎一直 0
- **MMR 真的实现了**：原版本 `enable_mmr/mmr_lambda` 是死代码，`_select` 完全没用
- **整段丢弃保结构**：`_compress` 不按行切，避免切断 `[Evidence]` 等节标题
- **dedent**：`[Output]` 用 `textwrap.dedent` 处理，无 12 空格缩进
- **错误观测**：所有工具调用异常用 `logger.exception(...)` 记录，不再 `print` 后吞掉
- **时区一致**：全程 UTC（`_now_utc`），naive datetime 兼容处理

### 7.3 工具结果判错

不做 `"未找到" not in result` 字面匹配——工具改提示语就坏。
让工具自己负责返回明确的空结果（None / 空串），`_normalize_tool_output` 统一识别。

---

## 八、扩展指引

### 8.1 增加新的信息源

最小改动是直接构造 `ContextPacket` 通过 `additional_packets` 传入：

```python
extra = ContextPacket(
    content="...",
    priority=ContextPriority.P2_EVIDENCE,
    metadata={"source": "my_custom_source"},
)
builder.build(user_query=..., additional_packets=[extra])
```

如果是稳定的、每次都要触发的检索（例如新增一个"历史工单库"），
继承 `ContextBuilder` 并扩展 `_gather_sync` / `_gather_async`：

```python
class MyBuilder(ContextBuilder):
    def _gather_sync(self, **kw):
        packets = super()._gather_sync(**kw)
        # 追加自定义来源
        ticket_packet = self._search_my_tickets(kw["user_query"])
        if ticket_packet:
            packets.append(ticket_packet)
        return packets
```

### 8.2 替换相关性算法

继承并覆盖 `_select` 的 jaccard 计算，或者覆盖 `_mmr_rerank`。
模块级 `tokenize_for_relevance` 的输出是 `FrozenSet[int]`（tiktoken token id）。
若想接 embedding 余弦相似度，把 `tokenize_for_relevance` 换成向量化函数、
`jaccard` 换成余弦相似度即可，整体接口不变。

### 8.3 替换节模板

继承并覆盖 `_structure`。注意保持 `_SECTION_HEADERS` 中的节标题与
`_compress` 的丢弃顺序对应，否则压缩会失效。

### 8.4 调参实操建议

- prompt 偏短（< 4000 token）：`reserve_ratio=0.1` 即可，留生成余量更小
- 业务对相关性要求严：`min_relevance` 调到 0.1+，但要先用 `build_detailed` 看哪些被误丢
- 历史很长：`history_max_messages` 调到 5–8 + 关 P3 不必要的展开
- 多个相似 RAG 召回：`mmr_lambda` 降到 0.5–0.6，多样性优先

### 8.5 单元测试

参考 [`test/test_context_builder.py`](../test/test_context_builder.py)，10 组用例覆盖：

```bash
cd cb-agent
PYTHONIOENCODING=utf-8 ../venv/python.exe test/test_context_builder.py
```

mock 工具用 `_MockMemoryTool` / `_MockRagTool`，无需真实依赖。
