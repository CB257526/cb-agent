# cb-agent

> 一个轻量、可读、可拆解的 LLM Agent 框架。  
> 把"上下文工程、原生工具、MCP、记忆、RAG、Skills"全部组装成一个可跑通的多轮 Function Calling 主循环。

```
你的输入  ─▶  ContextBuilder（GSSC）─▶  LLM 流式 think  ─▶  Function Calling
                  ▲                          │
                  │                          ▼
            Memory/RAG/History         tools + MCP + skills
```

---

## 特性

- **多轮工具循环**：流式 Function Calling，按 `index` 拼回 `tool_calls` 分片，自动回灌 `reasoning_content`（兼容 DeepSeek thinking 模式）
- **GSSC 上下文流水线**：Gather → Select（相关性 + 时近性 + MMR 去冗余）→ Structure → Compress，超预算时按节丢弃保结构
- **三层记忆**：Episodic / Semantic / Working，向量库（zvec / Qdrant）+ 图库（SQLite / Neo4j）双引擎
- **多模态 RAG**：text / image / audio 统一管道，OCR/ASR 落到文本侧再向量化
- **MCP 一等公民**：`mcp.json` 通过 `${VAR}` 占位符引用环境变量，启动期把每个 MCP 工具自动展开成独立 `Tool`
- **Skill = Prompt as Capability**：Markdown + YAML frontmatter 声明，三级懒加载（清单 → 全文 → 引用脚本），节省上下文
- **OpenAI 兼容**：DeepSeek / 阿里百炼 / 火山方舟 / 魔搭 / OpenAI / Ollama 任意切换
- **Claude Code 风格 REPL**：彩色 Todo 面板、`Thought for Xs` 折叠思考块、增量 messages dump

---

## 项目结构

```
cb-agent/
├── agent/                LLM 客户端封装（流式 Function Calling）
├── context/              ContextBuilder（GSSC 流水线）  ▶ context/README.md
├── core/                 Message / 数据库配置
├── memory/               记忆体系（episodic/semantic/working）
│   ├── rag/              多模态 RAG 管道                ▶ memory/rag/RAG_GUIDE.md
│   ├── storage/          向量 / 图存储                  ▶ memory/storage/VECTOR_STORE_GUIDE.md
│   └── types/            三种记忆类型
├── tools/                工具系统（原生 + MCP）           ▶ tools/TOOL_SYSTEM_DESIGN.md
│   ├── tools/            原生工具：memory / rag / search / todo / skill / run_skill_script
│   └── mcp_tools/        MCP 客户端 + 工具展开器
├── skills/               Skill 加载与执行                ▶ skills/SKILLS_GUIDE.md
├── constant/             模型常量
├── utils/                通用 / 多模态工具函数
├── .cbagent/skills/      项目级预置 Skill（pdf / skill-creator）
├── mcp.json              MCP 服务器声明（用 ${VAR} 占位密钥）
├── run_agent.py          交互式 REPL 入口
└── pyproject.toml
```

---

## 快速开始

### 1. 安装

```bash
git clone <your-fork-url> cb-agent && cd cb-agent
python -m venv venv
# Windows
venv\Scripts\activate
# macOS / Linux
source venv/bin/activate
```

依赖按场景分两档，按需安装：

| 档位 | 命令（任选其一） | 包含 | 不含 |
|------|------|------|------|
| **core**（最小） | `pip install -r requirements.txt`<br>`pip install -e .` | agent + context + 原生 tools + MCP，跑通 REPL | 本地 embedding、多模态、向量/图库、PDF、外部搜索 |
| **full**（完整） | `pip install -r requirements-full.txt`<br>`pip install -e ".[full]"` | 全部功能，含 torch / sentence-transformers / Qdrant / Neo4j / PDF / Tavily / SerpApi | — |

> Python ≥ 3.10。  
> MCP 服务器依赖 Node.js（`npx`），按需安装。  
> `full` 档因含 `torch` 体积较大（GB 级），如果只想用远程 embedding 服务（如阿里百炼 / Ollama）保持 core 即可，把 `EMBED_MODEL_TYPE=remote` 配到 `.env`。

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填上 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID
```

`.env` 已被 `.gitignore` 忽略。`mcp.json` 里通过 `${AMAP_MAPS_API_KEY}` 等占位符读环境变量，密钥不会进仓库。

### 3. 启动 REPL

```bash
python run_agent.py
```

斜杠命令：

| 命令 | 说明 |
|------|------|
| `/help` | 列出所有命令 |
| `/tools` | 当前注册的工具清单 |
| `/skills` | 已发现的 Skill |
| `/history` | 当前会话历史 |
| `/clear` | 清空历史 |
| `/ctx on\|off` | 开关 ContextBuilder |
| `/msg on\|off` | 开关每轮 messages 增量 dump |
| `/quit` | 退出 |

---

## 端到端示例

```python
from agent.cb_agents import CbAgentsLLM
from context import ContextBuilder, ContextConfig
from core.message import Message

llm = CbAgentsLLM()                  # 自动读 .env 选 provider
builder = ContextBuilder(ContextConfig(token_budget=4000))

history = [
    Message(role="user", content="我上周提过一个性能问题"),
    Message(role="assistant", content="嗯，是哪个服务？"),
]

messages = builder.to_messages(
    user_query="那个服务现在 P99 是多少？",
    system_instructions="你是一个运维助手，回答简洁。",
    history=history,
)

result = llm.think(messages, tools=None)   # 流式打印
print(result["answer"])
```

详细 API 参考各子模块文档：
- 上下文：[context/README.md](context/README.md)
- 工具：[tools/TOOL_SYSTEM_DESIGN.md](tools/TOOL_SYSTEM_DESIGN.md)
- Skill：[skills/SKILLS_GUIDE.md](skills/SKILLS_GUIDE.md)
- RAG：[memory/rag/RAG_GUIDE.md](memory/rag/RAG_GUIDE.md)
- 向量库：[memory/storage/VECTOR_STORE_GUIDE.md](memory/storage/VECTOR_STORE_GUIDE.md)
- 图库：[memory/storage/GRAPH_STORE_GUIDE.md](memory/storage/GRAPH_STORE_GUIDE.md)

---

## 工具一览

启动时由 `run_agent.py` 自动注册：

| 工具 | 用途 |
|------|------|
| `memory` | 三层记忆读写（自动选 episodic/semantic/working） |
| `rag` | 多模态文档检索（text/image/audio） |
| `my_advanced_search` | 多源 Web 搜索（Tavily / SerpApi） |
| `todo` | 任务分解与跟踪，REPL 里渲染成彩色面板 |
| `skill` | 加载并执行 Skill 完整指令 |
| `run_skill_script` | 执行 Skill 捆绑的 Python 脚本 |
| MCP 子工具 | `mcp.json` 中每个 server 的 tool 自动展开为独立工具 |

写自定义工具：继承 `tools.Tool`，实现 `get_parameters / validate_parameters / run`，详见 [tools/TOOL_SYSTEM_DESIGN.md](tools/TOOL_SYSTEM_DESIGN.md)。

---

## 内置 Skill

`.cbagent/skills/` 下两个示例：

- **pdf** — 读/合并/拆分/旋转/水印/加解密/OCR PDF
- **skill-creator** — 用对话方式创建、改进、评估你自己的 Skill

写自定义 Skill：在 `.cbagent/skills/<name>/SKILL.md` 写 frontmatter + 流程，引用脚本放同目录。详见 [skills/SKILLS_GUIDE.md](skills/SKILLS_GUIDE.md)。

---

## 设计要点

### 上下文工程：GSSC

不是简单 `messages.append(...)`，而是把"系统指令 / 任务态 / 历史 / 证据 / RAG"按优先级和预算装配。流程见 [context/README.md](context/README.md) §2.1。

```
user_query ─▶ Gather ─▶ Select ─▶ Structure ─▶ Compress ─▶ ctx (str)
                          (相关性+时近性+MMR)   (按优先级分节)  (按节丢弃保结构)
```

### 流式 Function Calling

`agent/cb_agents.py` 的 `_think_with_Function_Calling` 在 `stream=True` 下：

- `delta.content` 边收边打
- `delta.tool_calls` 按 `index` 累积分片（name 和 arguments 都可能被切多段）
- `delta.reasoning_content` 累积后单独透出，在终端渲染成 `▸ Thought for X.Xs` 折叠块

`run_agent.py` 在每轮 think 之间增量 dump `messages`（只打新增条），便于观察提示词组装。

### Tool / MCP / Skill 的边界

- **Tool**：原子化函数调用，参数由 OpenAI schema 定义
- **MCP**：外部进程 + 协议，启动期通过 `mcp.json` 拉起，每个工具自动包装成独立 `Tool`
- **Skill**：Prompt-as-Capability，Markdown 写工作流，按需注入提示词；可调用 Tool 完成具体动作

---

## 开发

```bash
python -m pytest test/ -q              # 上下文模块单测
python test.py                         # 上下文 + LLM 端到端示例
python run_agent.py                    # 完整 REPL
```

环境变量诊断：启动后先 `/tools` 看注册的工具数量，`/skills` 看 Skill 是否被发现。

---

## 路线图

- [ ] 异步工具循环（`asyncio` + 并行多个 `tool_calls`）
- [ ] 更细粒度的 token 预算（区分 system / history / evidence）
- [ ] 内置评估框架（让 `skill-creator` 能跑回归）
- [ ] Web UI（流式 SSE + 工具调用可视化）

---

## License

MIT
