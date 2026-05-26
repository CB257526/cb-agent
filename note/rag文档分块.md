文档分块是 RAG 系统中**最关键也最容易被低估的环节**。分块质量直接影响检索精度和大模型回答的完整性。让我从原理到实践系统讲解。

---

## 一、为什么要分块？

### 1. 上下文窗口限制
LLM 的上下文窗口有限（如 128K），不可能一次塞进整本《三国演义》。必须把文档切成小块，只喂给模型最相关的部分。

### 2. 检索精度
如果块太大（比如一章5000字），可能包含太多噪音；如果块太小（每句20字），可能丢失上下文。**分块的目标是找到语义完整的"最小可检索单元"**。

### 3. 向量嵌入质量
嵌入模型通常也有输入长度限制（如 BERT 的 512 token），超长的文本会被截断，导致信息丢失。

---

## 二、核心分块策略

### 1. 固定大小分块（Fixed-size Chunking）
最简单粗暴的方式。

```python
text = "这是一段很长的文本..."
chunk_size = 500  # 每块500字
overlap = 100     # 重叠100字

chunks = []
for i in range(0, len(text), chunk_size - overlap):
    chunk = text[i:i + chunk_size]
    chunks.append(chunk)
```

**优点**：简单、确定性强
**缺点**：经常在句子中间切断，破坏语义完整性

---

### 2. 基于分隔符的递归分块（Recursive Character Splitting）
这是 **LangChain 默认策略也是本项目的策略**，也是实战中最常用的。

```python
from langchain.text_splitter import RecursiveCharacterTextSplitter

splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=100,
    separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""]
    # 优先用段落分隔，然后是句子分隔，最后才是字符
)
```

**工作逻辑**：
```
文档 → 尝试用 "\n\n" 分割
    ↓ 如果还是太大
    尝试用 "\n" 分割
    ↓ 如果还是太大
    尝试用 "。"（中文句号）分割
    ↓ ...
    直到每块都在 chunk_size 以内
```

**优点**：尽量保持语义完整性
**缺点**：对无结构文本效果一般

---

### 3. 语义分块（Semantic Chunking）
基于**语义相似度**来决定切分点，是目前最先进的策略之一。

```python
# 原理示意
sentences = split_sentences(document)
embeddings = [embed(s) for s in sentences]

chunks = []
current_chunk = [sentences[0]]

for i in range(1, len(sentences)):
    similarity = cosine_similarity(embeddings[i-1], embeddings[i])
    
    if similarity > threshold:  # 语义连续，继续追加
        current_chunk.append(sentences[i])
    else:  # 语义断裂，开始新块
        chunks.append(" ".join(current_chunk))
        current_chunk = [sentences[i]]
```

**核心思路**：当两个句子的语义相似度突然下降时，说明话题切换了，这里就是最佳切分点。

**优点**：真正的"按话题"分块
**缺点**：计算量大，需要为每句话生成嵌入

---

### 4. 基于文档结构的分块（Structural Chunking）
充分利用文档自身的结构信息。

**Markdown 文档**：
```markdown
# 第一章：Python基础         ← H1 是新块开始
## 1.1 变量与类型            ← H2 作为块的元数据
正文内容...
### 1.1.1 整数类型           ← H3 可以保留或合并
```

```python
# 将 Markdown 按标题层级分块
from langchain.text_splitter import MarkdownHeaderTextSplitter

headers_to_split_on = [
    ("#", "h1"),
    ("##", "h2"),
    ("###", "h3"),
]

splitter = MarkdownHeaderTextSplitter(headers_to_split_on)
chunks = splitter.split_text(markdown_text)

# 每个chunk会带上标题元数据
# chunk.metadata = {"h1": "第一章", "h2": "1.1 变量与类型"}
```

**PDF 文档**：
- 利用 PDF 的段落标记、字体大小变化
- 表格单独处理，保留结构化格式

---

### 5. Agentic 分块（智能分块）
让 LLM 来决策切分点，特别适合非结构化、跨话题的文档。

```python
prompt = """请将以下文档按话题切分，每个话题一个块。
输出格式：用 <CHUNK_SPLIT> 标记切分点。

文档：
{文档内容}
"""

response = llm.generate(prompt)
chunks = response.split("<CHUNK_SPLIT>")
```

**优点**：智能程度最高，可以处理任意格式
**缺点**：成本高、速度慢、结果可能不稳定

---

## 三、Chunk Overlap（重叠）的作用

重叠是防止信息在边界丢失的关键机制。

```
原文：   [A B C D E F G H I J]
块1：    [A B C D E F]          ← 包含 A-F
块2：          [D E F G H I J]  ← 包含 D-J，与块1重叠 D-E-F
```

```python
# 为什么需要重叠？
# 问题：用户问"E和F的关系"
# 如果无重叠：E在块1，F在块1 → 能回答 ✅
# 如果无重叠：E在块1尾，F在块2头 → 任何一块都不完整 ❌
# 有重叠：E和F同时出现在两块中 → 至少有一块是完整的 ✅
```

**经验值**：重叠量通常是 chunk_size 的 10%-20%。

---

## 四、实战中的分块决策指南

| 文档类型 | 推荐策略 | 典型参数 | 原因 |
|---------|---------|---------|------|
| 技术文档/Markdown | **结构化+递归** | 1000 token, 重叠200 | 层级清晰，保留标题上下文 |
| 法律合同 | **固定大小+小chunk** | 300 token, 重叠50 | 每条款独立，需精确匹配 |
| 小说/文章 | **语义分块** | 1500 token, 重叠200 | 按情节/话题分段 |
| 对话记录 | **Agentic分块** | 2000 token, 重叠300 | 话题切换频繁，需智能判断 |
| 论文/学术 | **结构化+语义** | 800 token, 重叠150 | 有章节结构，但段落内语义密集 |
| 混合文档库 | **递归分块（默认）** | 500-1000 token | 通用性强，适应各种格式 |

---

## 五、高级技巧

### 1. 上下文窗口的"父子块"策略（Parent-Child Chunking）

```python
# 检索时用小块（精确匹配），喂给LLM时用大块（保留上下文）
small_chunks = split(text, chunk_size=300)   # 用于向量检索
large_chunks = split(text, chunk_size=2000)  # 用于生成回答

# 每个小块记录它所属的大块ID
small_chunk.metadata["parent_chunk_id"] = parent_id
```

### 2. 基于摘要的块增强

为每个块生成一句话摘要，存到元数据里：

```python
chunk.metadata["summary"] = "本章讨论Python的多线程编程"
chunk.metadata["keywords"] = ["Python", "多线程", "GIL"]
```

检索时，摘要也参与相似度计算，提升召回率。

### 3. 特殊内容的"零分块"处理

| 内容类型 | 处理方式 |
|---------|---------|
| 表格 | 转换成 Markdown 表格格式，不分块 |
| 代码 | 保持完整函数/类，不分块 |
| 数学公式 | 保留 LaTeX 原格式，不分割 |

---

## 总结

分块操作的本质是：**在"检索精度"和"语义完整"之间找到平衡点**。没有放之四海皆准的方案，需要根据你的具体文档类型、检索场景、模型上下文窗口来调优。最好的做法是从**递归分块（LangChain默认策略）**开始，然后根据实际检索效果，逐步引入语义分块、结构化分块等高级策略。