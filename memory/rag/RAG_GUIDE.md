# RAG（检索增强生成）系统文档

## 1. 系统概述

RAG 系统为 HelloAgents 框架提供**多模态检索增强生成**能力。它将用户的文档、图片、音频等数据统一转化为可搜索的知识库，在 Agent 需要时检索相关内容，注入 LLM 提示词，生成基于真实数据的准确回答。

### 核心能力

| 模态 | 入库方式 | 搜索方式 | 返回内容 |
|---|---|---|---|
| 文本文档 | MarkItDown 解析 → 智能分块 → 向量嵌入 | 文本查询 → 向量相似度检索 | 相关文本片段 + 来源文件路径 |
| 图片 | 视觉 LLM OCR 识别 → 文本嵌入 | 文本查询 → 向量检索 | 识别文字/描述 + 原始图片路径 |
| 音频 | 语音识别 LLM ASR 转录 → 文本嵌入 | 文本查询 → 向量检索 | 转录文本 + 原始音频路径 |

### 设计原则

- **统一文本化**：所有模态（文本、OCR 文字、ASR 转录）最终都转为文本，由同一向量嵌入模型处理
- **元数据保留**：原始文件路径保存在每条记录的元数据中，搜索时与文本一并返回，供 Agent 展示给用户
- **后端可换**：向量存储通过 VectorStoreManager 自动适配 Zvec（默认）/Qdrant，图存储通过 GraphStoreManager 适配 SQLite（默认）/Neo4j

---

## 2. 快速开始

```python
from tools.tools.rag_tool import RAGTool

# 1. 初始化（自动从 .env 读取数据库和嵌入模型配置）
rag = RAGTool()

# 2. 添加文档
rag.run({"action": "add_document", "file_path": "报告.pdf"})

# 3. 添加图片（需要 OCR 配置）
rag.run({"action": "add_images", "file_path": "截图.png"})

# 4. 添加音频（需要 ASR 配置）
rag.run({"action": "add_audio", "file_path": "录音.mp3"})

# 5. 智能问答
answer = rag.run({"action": "ask", "question": "这份报告的核心结论是什么？"})
```

---

## 3. RAGTool 工具使用

### 3.1 初始化参数

```python
RAGTool(
    knowledge_base_path="./knowledge_base",  # 临时文件存储目录
    qdrant_url=None,                         # Qdrant 地址（None 时自动从环境变量读取）
    qdrant_api_key=None,                     # Qdrant API Key
    collection_name="rag_knowledge_base",    # 向量集合名称
    rag_namespace="default",                 # 默认命名空间
)
```

### 3.2 操作一览

| action | 用途 | 必需参数 | 可选参数 |
|---|---|---|---|
| `add_document` | 添加文本文档（PDF/Word/代码等） | `file_path` | `chunk_size`, `chunk_overlap`, `namespace` |
| `add_text` | 添加纯文本 | `text` | `document_id`, `namespace` |
| `add_images` | 添加图片（OCR 识别后入库） | `file_path` 或 `file_paths` | `namespace` |
| `add_audio` | 添加音频（ASR 转录后入库） | `file_path` 或 `file_paths` | `namespace` |
| `search` | 搜索文本知识库 | `query` | `limit`, `namespace`, `min_score` |
| `search_images` | 搜索图片知识库 | `query` | `limit`, `namespace` |
| `search_audio` | 搜索音频知识库 | `query` | `limit`, `namespace` |
| `ask` | 基于知识库的智能问答 | `question` 或 `query` | `limit`, `namespace` |
| `stats` | 获取知识库统计 | 无 | `namespace` |
| `clear` | 清空知识库（需确认） | `confirm=true` | `namespace` |

### 3.3 详细示例

#### 添加文本文档

```python
# 支持 PDF、Word、Excel、PPT、代码文件等
rag.run({
    "action": "add_document",
    "file_path": "技术白皮书.pdf",
    "chunk_size": 800,       # 每个分块的目标 Token 数
    "chunk_overlap": 100,    # 相邻分块重叠的 Token 数
    "namespace": "技术文档",
})
```

#### 添加图片

```python
# 单张图片
rag.run({
    "action": "add_images",
    "file_path": "架构图.png",
    "namespace": "设计资料",
})

# 批量图片
rag.run({
    "action": "add_images",
    "file_paths": ["截图1.png", "截图2.jpg", "图表3.png"],
    "namespace": "设计资料",
})
```

#### 添加音频

```python
rag.run({
    "action": "add_audio",
    "file_path": "会议录音.mp3",
    "namespace": "会议记录",
})
```

#### 搜索知识库

```python
# 搜文本
result = rag.run({"action": "search", "query": "微服务架构", "limit": 5})

# 搜图片（会返回 OCR 识别的文字 + 原始图片路径）
result = rag.run({"action": "search_images", "query": "系统架构图"})
# 返回示例：
# 1. 架构图.png (相似度: 0.433)
#    路径: C:\...\架构图.png
#    格式: image/png
#    内容: 图中的文字：API Gateway → Service A → Database...

# 搜音频（会返回 ASR 转录的文本 + 原始音频路径）
result = rag.run({"action": "search_audio", "query": "项目进度讨论"})
```

#### 智能问答

```python
result = rag.run({
    "action": "ask",
    "question": "这份简历中提到的技术栈有哪些？",
    "limit": 5,            # 检索的上下文片段数
    "namespace": "简历库",
})
```

---

## 4. 数据管线详解

### 4.1 文本处理管线

文本处理是 RAG 系统的核心，负责将任意格式的文档转化为可搜索的向量。

```
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  原始文档     │ → │ MarkItDown    │ → │ 智能分块      │ → │ 向量嵌入     │
│  PDF/Word/   │    │ 统一转       │    │ 标题感知分割  │    │ 百炼API/     │
│  Markdown/   │    │ Markdown     │    │ Token预算控制 │    │ sentence-    │
│  代码文件     │    │              │    │ 重叠区保留    │    │ transformers │
└──────────────┘    └──────────────┘    └──────────────┘    └──────┬───────┘
                                                                    │
                                                                    ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  搜索结果     │ ← │  向量检索     │ ← │  元数据写入   │ ← │  向量库      │
│  文本+来源    │    │  相似度排序  │    │  memory_type  │    │  Zvec/Qdrant │
│              │    │  模态过滤    │    │  rag_namespace│    │              │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

#### 阶段 1：文档转换为 Markdown — `_convert_to_markdown()`

使用微软开源的 [MarkItDown](https://github.com/microsoft/markitdown) 库，将 PDF、Office 文档等数十种格式统一转换为 Markdown 纯文本。

- **PDF 文件**：走增强处理管线，包含去噪 → 短行合并 → 段落重组三阶段
- **其他格式**：直接调用 MarkItDown 转换
- **兜底策略**：MarkItDown 不可用时，以 UTF-8 原始读取

#### 阶段 2：标题感知的段落拆分 — `_split_paragraphs_with_headings()`

将 Markdown 文本按标题层级（`#`、`##`、`###`）和段落分隔拆分。每个段落记录：

```python
{
    "content": "段落文本内容",
    "heading_path": "第一章 > 1.1 概述 > 1.1.1 架构",  # 所在标题路径
    "start": 120,   # 原文起始字符位置
    "end": 350,     # 原文结束字符位置
}
```

这保证了后续分块不会跨章节截断，且搜索结果可以附带所在章节信息。

#### 阶段 3：Token 预算分块 — `_chunk_paragraphs()`

以**段落为最小粒度**（不跨段落切断），按 Token 预算拼装分块：

- **chunk_tokens=800**：每个分块累计至多 800 Token
- **overlap_tokens=100**：相邻分块间保留尾部 100 Token 作为重叠区，避免关键上下文被截断
- **Token 估算**：CJK 字符 1 字符 ≈ 1 Token，英文按空白分词

```
段落 1 (120t) 段落 2 (350t) 段落 3 (400t)  段落 4 (200t)
├─ 分块 1: 段落1+2+3 (870t) ─┤
                  ├─ 分块 2: 段落3+4 (600t) ─┤  ← 段落3 是重叠区
```

#### 阶段 4：向量嵌入 — `index_chunks()`

- 嵌入前对 Markdown 文本预处理：去除 `##`、`**`、`` ` `` 等标记符号，保留纯语义文本
- 分批调用嵌入模型（默认 batch_size=64），支持失败重试（缩至 8 条/批，2 秒冷却）
- 维度不匹配时自动填充零向量或截断

#### 阶段 5：向量写入

写入元数据包含：

| 字段 | 说明 | 用于 |
|---|---|---|
| `memory_type: "rag_chunk"` | 标识为 RAG 数据 | 与记忆系统隔离 |
| `is_rag_data: "true"` | RAG 数据标记 | 过滤非 RAG 数据 |
| `data_source: "rag_pipeline"` | 数据来源标识 | 来源追溯 |
| `rag_namespace` | 命名空间 | 多项目隔离 |
| `modality` | 模态（image/audio/text） | 按模态过滤搜索 |
| `source_path` | 原始文件路径 | 结果中展示来源 |

### 4.2 搜索管线

```
查询文本 → 文本嵌入 → 向量检索 → [可选] 模态过滤 → [可选] 命名空间过滤 → 返回 top_k 结果
```

搜索结果每条包含：
```python
{
    "id": "chunk_id",
    "score": 0.573,            # 向量相似度分数
    "metadata": {
        "content": "具体内容...",    # 分块文本
        "source_path": "文档.pdf",   # 来源文件
        "modality": "image",         # 模态
        "original_file_path": "...", # 原始文件绝对路径（图片/音频）
        "heading_path": "..."        # 章节标题路径（文本文档）
    }
}
```

---

## 5. 多模态数据处理

### 5.1 架构

多模态数据通过 `MultimodalProcessor`（`utils/multimodal.py`）统一处理，其核心逻辑是：

**将非文本数据转为文本描述，然后用与文本完全相同的向量管线处理。**

```
图片文件                音频文件
   │                      │
   ▼                      ▼
┌──────────────┐    ┌──────────────┐
│ 视觉 LLM      │    │ 语音识别 LLM  │    ← MultimodalProcessor
│ OCR + 描述   │    │ ASR 转录     │
└──────┬───────┘    └──────┬───────┘
       │                   │
       ▼                   ▼
  文本描述              转录文本
  "图片中的文字："       "会议讨论内容："
       │                   │
       └─────┬─────────────┘
             │
             ▼
    ┌──────────────┐
    │  文本嵌入     │    ← 与文档处理相同
    │  向量存储     │
    │  元数据附加:  │
    │  - modality   │
    │  - 原始路径   │
    │  - MIME 类型  │
    └──────────────┘
```

### 5.2 图片处理（OCR）

**入口：** `MultimodalProcessor.process_image(file_path)`

**流程：**
1. 读取图片文件 → Base64 编码
2. 调用视觉 LLM（`qwen-vl-ocr`，通过 OpenAI 兼容接口）进行识别
3. LLM 被要求输出三部分：图中所有文字（逐字识别）、视觉内容描述、图片类型
4. 返回格式为：`{"text": "识别文本...", "metadata": {...}}`

**提示词策略：**
```
请详细描述这张图片的内容，包括：
1. 图片中的所有文字内容（逐字识别，不要遗漏）
2. 图片的视觉内容描述（场景、物体、人物、颜色、布局等）
3. 图片的类型（如：截图、照片、图表、海报等）
```

这样产生的文本既包含精确的文字 OCR，又包含语义描述。用户搜索"架构图"时，即使图中没有"架构图"这三个字，LLM 的视觉描述中也会包含"系统架构图"这样的描述，提高了搜索的语义匹配率。

**配置环境变量：**
```
OCR_API_KEY=sk-xxx
OCR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OCR_MODEL_NAME=qwen-vl-ocr-2025-11-20
```

### 5.3 音频处理（ASR）

**入口：** `MultimodalProcessor.process_audio(file_path)`

**流程：**
1. 读取音频文件 → Base64 编码
2. 调用语音识别 LLM（`qwen3-asr-flash`，通过 OpenAI 兼容接口）
3. 使用 `asr_options` 控制转录行为（关闭逆文本正则化，保留原始识别结果）
4. 返回格式为：`{"text": "转录文本...", "metadata": {...}}`

**配置环境变量：**
```
ASR_API_KEY=sk-xxx
ASR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ASR_MODEL_NAME=qwen3-asr-flash
```

### 5.4 多模态搜索与返回

搜索图片/音频时，返回结果中的关键字段：

```python
# search_images 返回示例
{
    "score": 0.433,
    "metadata": {
        "content": "OCR识别的文字和视觉描述...",     # 可搜索的文本
        "modality": "image",                       # 模态标识
        "original_file_path": "C:\\...\\截图.png",  # 原始文件绝对路径
        "mime_type": "image/png",                  # 文件类型
        "source_path": "C:\\...\\截图.png"          # 来源文件
    }
}
```

Agent 收到这个结果后，可以将 `original_file_path` 路径展示给用户（如直接打开图片、播放音频），同时展示 `content` 中的文本内容作为语义匹配的证据。

### 5.5 与记忆系统感知记忆的区别

| | RAG 图片处理 | 感知记忆（perceptual） |
|---|---|---|
| 编码方式 | OCR → 文本嵌入 | CLIP 视觉编码 / 哈希兜底 |
| 存储 | 与文本同在一个向量集合 | 按模态分 3 个独立集合 |
| 搜索 | 文本查询匹配 OCR 文字 | 需要 CLIP 跨模态对齐 |
| 优势 | 语义更丰富（含视觉描述） | 支持真正的视觉语义搜索 |
| 依赖 | 视觉 LLM API | CLIP/CLAP 模型文件 |

---

## 6. 高级搜索功能

### 6.1 多查询扩展（MQE）

将用户查询交给 LLM，生成多个语义等价的变体查询，分别检索后合并去重。提高对短查询、模糊查询的召回率。

```
用户查询: "Python 性能优化"
           │
           ▼
LLM 扩展: ["Python 代码加速方法", "Python 程序 profiling", "如何提高 Python 运行速度"]
           │
           ▼
分别检索 → 合并去重（保留每个文档最高分）
```

### 6.2 假设文档嵌入（HyDE）

让 LLM 先生成一段假设的答案文本，用这段文本做向量检索。原理是答案文本的向量分布更接近知识库中的实际文档。

```
用户问题: "微服务之间如何通信？"
           │
           ▼
LLM 生成假设答案: "微服务间通信主要有同步方式（HTTP/REST、gRPC）和异步方式（消息队列如Kafka、RabbitMQ）..."
           │
           ▼
用这段答案做向量检索 → 匹配到知识库中真实相关的文档段落
```

### 6.3 Cross-Encoder 重排序

向量检索（双塔模型）速度快但精度有限。Cross-Encoder 同时接受 (query, document) 对输入，输出精确相关度分数。通常在向量检索的 top-k 候选上运行。

### 6.4 文档结构图信号

利用同一文档内分块的位置关系增强排序：

- **同文档密度分数**：同一文档被命中的分块越多，该文档的每个命中分块得分越高
- **邻近性分数**：文档内位置越靠近的两段命中，互相增强

---

## 7. 存储后端配置

### 7.1 向量存储

通过 `VectorStoreManager` 自动选择后端：

| 后端 | 类型 | 配置方式 |
|---|---|---|
| Zvec | 本地嵌入式（默认） | VECTOR_STORE_TYPE 不设置或为 zvec |
| Qdrant | 远程云服务 | `VECTOR_STORE_TYPE=qdrant` + `QDRANT_URL` + `QDRANT_API_KEY` |

### 7.2 图存储

通过 `GraphStoreManager` 自动选择后端：

| 后端 | 类型 | 配置方式 |
|---|---|---|
| SQLite | 本地嵌入式（默认） | GRAPH_STORE_TYPE 不设置或为 sqlite |
| Neo4j | 远程图数据库 | `GRAPH_STORE_TYPE=neo4j` + `NEO4J_URI` + `NEO4J_USERNAME` + `NEO4J_PASSWORD` |

### 7.3 Zvec 的 RAG 专用 Schema

Zvec 在创建集合时会定义所有字段。为支持 RAG 的过滤查询，Schema 中包含以下索引字段：

```
memory_id     STRING  倒排索引  ← 按 ID 删除
user_id       STRING  倒排索引  ← 多用户隔离
memory_type   STRING  倒排索引  ← 类型过滤（rag_chunk / episodic / ...）
is_rag_data   STRING  倒排索引  ← RAG数据标识
data_source   STRING  倒排索引  ← 数据来源
rag_namespace STRING  倒排索引  ← 命名空间隔离
modality      STRING  倒排索引  ← 模态过滤（text/image/audio）
content       STRING  仅存储    ← 原始文本
importance    FLOAT   仅存储    ← 重要性
timestamp     INT64   仅存储    ← 时间戳
payload_json  STRING  仅存储    ← 额外元数据的JSON序列化
embedding     VECTOR  HNSW索引  ← 向量（1024维，余弦距离）
```

---

## 8. 文件结构

```
memory/rag/
  ├── __init__.py               # 模块导出
  ├── document.py               # DocumentProcessor 文档预处理
  └── pipeline.py               # RAG 核心管线

utils/
  └── multimodal.py             # 多模态处理器（OCR + ASR）

tools/tools/
  └── rag_tool.py               # RAGTool 工具实现

test/
  └── test_rag_operations.py    # 综合测试脚本
```

### 8.1 pipeline.py 函数清单

#### 文档加载与分块
| 函数 | 说明 |
|---|---|
| `load_and_chunk_texts()` | 通用文档加载 → Markdown 转换 → 分块 |
| `_convert_to_markdown()` | 多格式 → Markdown（PDF 走增强处理） |
| `_split_paragraphs_with_headings()` | 标题感知段落拆分 |
| `_chunk_paragraphs()` | Token 预算分块 + 重叠 |
| `_preprocess_markdown_for_embedding()` | 嵌入前的 Markdown 清洗 |

#### 向量索引与搜索
| 函数 | 说明 |
|---|---|
| `index_chunks()` | 分块嵌入 → 向量入库 |
| `embed_query()` | 查询文本 → 向量编码 |
| `search_vectors()` | 向量相似度检索（支持模态过滤） |
| `search_vectors_expanded()` | 增强搜索（MQE + HyDE） |

#### 多模态
| 函数 | 说明 |
|---|---|
| `index_image()` | 单张图片 OCR → 入库 |
| `index_audio()` | 单个音频 ASR → 入库 |
| `load_and_index_images()` | 批量图片处理 |
| `load_and_index_audio()` | 批量音频处理 |
| `search_images()` | 图片知识库检索 |
| `search_audio()` | 音频知识库检索 |

#### 排序与合并
| 函数 | 说明 |
|---|---|
| `rerank_with_cross_encoder()` | Cross-Encoder 精细重排序 |
| `compute_graph_signals_from_pool()` | 文档结构图信号计算 |
| `rank()` | 向量分 + 图信号融合排序 |
| `merge_snippets()` | 简单合并（按字符数截断） |
| `merge_snippets_grouped()` | 按文档分组合并（带引用标注） |
| `compress_ranked_items()` | 压缩结果（合并相邻+限制每文档数量） |
| `expand_neighbors_from_pool()` | 扩展邻近分块 |

---

## 9. 配置参考

### 9.1 环境变量

```bash
# ── 嵌入模型 ──
EMBEDDING_MODEL_TYPE=ollama        # openai / ollama / local
EMBEDDING_MODEL_NAME=qwen3-embedding:0.6b
EMBEDDING_BASE_URL=http://localhost:11434/v1

# ── OCR 图片识别 ──
OCR_API_KEY=sk-xxx
OCR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
OCR_MODEL_NAME=qwen-vl-ocr-2025-11-20

# ── ASR 语音识别 ──
ASR_API_KEY=sk-xxx
ASR_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ASR_MODEL_NAME=qwen3-asr-flash

# ── 向量存储 ──
VECTOR_STORE_TYPE=zvec              # zvec（默认）或 qdrant
QDRANT_URL=                         # 仅 Qdrant 需要
QDRANT_API_KEY=                     # 仅 Qdrant 需要

# ── 图存储 ──
GRAPH_STORE_TYPE=sqlite             # sqlite（默认）或 neo4j
NEO4J_URI=                          # 仅 Neo4j 需要
NEO4J_USERNAME=                     # 仅 Neo4j 需要
NEO4J_PASSWORD=                     # 仅 Neo4j 需要
```

### 9.2 可调参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `chunk_size` | 800 | 每个分块的目标 Token 数 |
| `chunk_overlap` | 100 | 相邻分块重叠的 Token 数 |
| `batch_size` | 64 | 嵌入模型批处理大小 |
| `top_k` | 5 (ask) / 8 (search) | 返回结果数量 |
| `max_chars` | 1200 | 合并上下文的最大字符数 |
| `min_score` | 0.1 | 最低相似度阈值 |
| `enable_advanced_search` | True | 是否启用 MQE + HyDE 增强搜索 |
