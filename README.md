# cb-agent

> 一个可拆解、可审计、带工具循环的 LLM Agent 框架。
> 默认走轻量 Markdown 记忆；需要旧向量记忆/RAG 时再显式启用 full 模式。

```
用户输入
  └─> ContextBuilder(GSSC)
        ├─ system / session state / Markdown memory / history
        ├─ full memory + RAG（可选）
        └─ tools + MCP + skills
  └─> LLM stream think
  └─> Function Calling 工具循环
  └─> final answer + 工作记录 + 本地会话持久化
```

## 功能概览

| 能力 | 说明 |
|---|---|
| 多轮 Function Calling | 支持流式内容、reasoning delta、tool_calls 分片累积与 tool result 回灌 |
| ContextBuilder | GSSC：Gather → Select → Structure → Compress，按优先级和 token 预算组织上下文 |
| 轻量 Markdown 记忆 | 默认启用，使用 `~/.cbagent/memory/` 与项目 `.cbagent/memory/`，不依赖 embedding / 向量库 |
| full 记忆/RAG | 旧 `MemoryTool` / `RAGTool` 完整保留，通过 `--memory-system full` 启用 |
| 跨轮工作上下文 | 每轮工具轨迹压成 `【工作记录】`，写入 `.cbagent/sessions/`，重启后可恢复 |
| 多会话隔离 | 本地 session 可创建、切换、清理；不同会话 history/state/transcript 隔离 |
| 上下文压缩 | TUI 支持 `/compact`；后端也会在上下文接近模型窗口 80% 时自动 compact |
| TUI | Ink/React 终端界面，支持工具卡片、会话切换、Context 占用指标、日志面板 |
| MCP | 读取 `mcp.json`，通过 stdio 启动 MCP server，并展开成可调用工具 |
| Skills | Markdown + YAML frontmatter 描述能力，按需加载指令和脚本 |
| Bash 权限 | Bash 工具支持权限语义，TUI 模式下权限确认走 UI 通道 |

## Quickstart

### 0. 准备环境

必需：

- Python `>=3.10`
- 一个 OpenAI-compatible LLM 服务，至少提供 `LLM_MODEL_ID`、`LLM_API_KEY`、`LLM_BASE_URL`

如果要使用 TUI：

- Node.js `>=20`
- npm

如果要使用 MCP：

- 能运行 `npx`
- `mcp.json` 中相关服务需要的环境变量，例如 `AMAP_MAPS_API_KEY`

### 1. 克隆并进入项目

```bash
git clone <your-fork-url> cb-agent
cd cb-agent
```

如果你当前就在本仓库，路径通常类似：

```powershell
cd C:\Users\cb135\Desktop\cbAgent\cb-agent
```

### 2. 创建 Python 虚拟环境

Windows PowerShell：

```powershell
python -m venv ..\venv
..\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

macOS / Linux：

```bash
python -m venv ../venv
source ../venv/bin/activate
python -m pip install --upgrade pip
```

> TUI 会优先查找 `../venv/python.exe`、`../venv/Scripts/python.exe` 或 `../venv/bin/python`，所以推荐把虚拟环境放在项目父目录的 `venv`。

### 3. 安装依赖

默认轻量安装：

```bash
pip install -e .
```

或使用 requirements：

```bash
pip install -r requirements.txt
```

完整安装，启用旧向量记忆/RAG、多模态 RAG、PDF 等可选依赖。Web 搜索只需要核心依赖里的 `requests` 和对应 API Key：

```bash
pip install -e ".[full]"
```

或：

```bash
pip install -r requirements-full.txt
```

两种安装模式的区别：

| 模式 | 安装命令 | 默认记忆 | 是否注册 `memory` / `rag` | 适合场景 |
|---|---|---|---|---|
| light | `pip install -e .` | Markdown 文件 | 否 | 先跑起来、低依赖、无需向量库 |
| full | `pip install -e ".[full]"` | 旧向量记忆/RAG | 是 | 需要 embedding、RAG、多模态、向量/图存储 |

### 4. 配置 `.env`

复制模板：

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

macOS / Linux：

```bash
cp .env.example .env
```

至少填写：

```env
LLM_MODEL_ID=deepseek-v4-flash
LLM_API_KEY=your_api_key
LLM_BASE_URL=https://your-provider.example/v1
LLM_TIMEOUT=60
```

注意：`LLM_MODEL_ID` 必须在 [constant/llm/constant_llm.py](constant/llm/constant_llm.py) 的 `ConstantLLM.llm_dict` 里登记。新增模型时要补：

- `is_tool`
- `is_reasoning`
- `json_output`
- `max_tokens`
- `image_ability`

可选环境变量：

| 变量 | 用途 |
|---|---|
| `AMAP_MAPS_API_KEY` | `mcp.json` 里的高德 MCP server |
| `TAVILY_API_KEY` | `my_advanced_search` 的 Tavily 搜索源 |
| `SERPAPI_API_KEY` | `my_advanced_search` 的 SerpApi 搜索源 |
| `VECTOR_STORE_TYPE` / `QDRANT_URL` / `QDRANT_API_KEY` | full RAG/Memory 的向量存储 |
| `GRAPH_STORE_TYPE` / `NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD` | full 语义记忆图存储 |
| `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` / `EMBED_API_KEY` | full embedding 配置 |

`.env` 已被 `.gitignore` 忽略，不会进仓库。

### 5. 启动 CLI

默认启动，使用轻量 Markdown 记忆：

```bash
python run_agent.py
```

等价于：

```bash
python run_agent.py --memory-system light
```

常用启动参数：

```bash
python run_agent.py --memory-system off
python run_agent.py --memory-system full
python run_agent.py --no-mcp
python run_agent.py --no-ctx
```

CLI 里建议先跑：

```text
/tools
/skills
帮我看一下这个项目有哪些核心模块
```

退出：

```text
/quit
```

### 6. 启动 TUI

第一次进入 TUI 目录安装依赖：

```bash
cd ui-tui
npm install
```

启动：

```bash
npm start
```

TUI 会自动 spawn：

```bash
python run_agent.py --transport jsonrpc --memory-system light
```

如果 TUI 找不到 Python，可以指定：

Windows PowerShell：

```powershell
$env:CB_AGENT_PYTHON="C:\Users\cb135\Desktop\cbAgent\venv\Scripts\python.exe"
npm start
```

macOS / Linux：

```bash
CB_AGENT_PYTHON=/path/to/python npm start
```

TUI 快捷键：

| 快捷键 / 命令 | 说明 |
|---|---|
| `Enter` | 发送输入 |
| `/` | 打开 slash command picker |
| `/tools` | 列出后端注册工具 |
| `/sessions` | 打开本地会话切换面板 |
| `/new` | 新建并切换到空白会话 |
| `/switch <id>` | 切换到指定 session |
| `/compact` | 手动压缩当前会话上下文 |
| `/clear` | 清空前端显示并清空后端当前会话历史 |
| `/log` 或 `Ctrl-O` | 切换后端日志面板 |
| `Ctrl-L` | 清当前屏幕显示，保留后端 history |
| `Ctrl-C` | busy 时取消当前回答；空闲时退出 |

TUI 底部状态栏会显示：

- 当前模型
- 当前 session
- `Context used/max percent`，也就是 state + history 对上下文窗口的占用
- OpenAI usage 累计
- 工具循环 round

### 7. 验证安装是否正常

Python 侧：

```bash
python run_agent.py --help
python agent_run_basic.py --help
python test/test_context_builder.py
python test/test_session_renderer.py
python test/test_transport.py
```

Windows 控制台如果出现 `✓` 编码问题，使用：

```powershell
$env:PYTHONIOENCODING="utf-8"
python test/test_context_builder.py
```

TUI 侧：

```bash
cd ui-tui
npm test
npm run build
```

## 记忆系统

### light：默认 Markdown 记忆

light 模式不会 import / register 旧 `MemoryTool`、`RAGTool`，因此不需要 embedding、向量库或 RAG env。

目录：

| 级别 | 路径 | 用途 |
|---|---|---|
| 用户全局 | `~/.cbagent/memory/` | 长期偏好、跨项目事实 |
| 当前项目 | `.cbagent/memory/` | 当前仓库的约定、进展、参考 |

每个目录都有 `MEMORY.md` 索引，具体记忆写在同目录其它 `.md` 文件。记忆文件建议：

```markdown
---
name: 用户偏好
description: 用户希望回答使用中文并附验证命令
type: user
scope: global
---

用户偏好：回答使用中文，必要时说明验证命令。
```

light 模式不新增记忆工具。用户要求“记住/保存偏好/保存项目事实”时，agent 会通过现有 `file_read` / `file_write` 修改 Markdown 记忆文件。

### full：旧向量记忆与 RAG

完整安装后可启用：

```bash
python run_agent.py --memory-system full
```

full 模式会注册：

| 工具 | 说明 |
|---|---|
| `memory` | Episodic / Semantic / Working 三层记忆 |
| `rag` | 文档、多模态 RAG 检索 |

相关文档：

- [memory/rag/RAG_GUIDE.md](memory/rag/RAG_GUIDE.md)
- [memory/storage/VECTOR_STORE_GUIDE.md](memory/storage/VECTOR_STORE_GUIDE.md)
- [memory/storage/GRAPH_STORE_GUIDE.md](memory/storage/GRAPH_STORE_GUIDE.md)

### off：关闭长期记忆

```bash
python run_agent.py --memory-system off
```

off 模式不使用 Markdown 记忆，也不注册旧 `memory` / `rag`，但仍保留本地 session/history/state。

## 会话、工作记录与 compact

本地会话保存在：

```text
.cbagent/sessions/
```

主要文件：

| 文件 | 说明 |
|---|---|
| `index.json` | 当前 active session 指针 |
| `transcript.jsonl` | 每轮 user、final answer、工作记录、压缩 trace 审计 |
| `state.json` | rolling summary、已读文件、已改文件、最近命令、待办等 |
| `compact.json` | 最近一次 compact 快照 |
| `compactions.jsonl` | compact 审计事件 |

跨轮工具记录不会保存完整 file content 或完整 bash stdout。工具结果会压缩成 `【工作记录】`，下一轮作为普通 assistant history 进入上下文。

### 多会话

CLI 支持：

| 命令 | 说明 |
|---|---|
| `/sessions` | 列出本项目本地会话 |
| `/new` | 新建并切换到空白会话 |
| `/switch <session_id>` | 切换到指定会话 |
| `/clear` | 删除当前 active session 文件并清空内存 history |

TUI 支持 `/sessions` 面板、`/new`、`/switch <id>`。

### 手动 compact

TUI 支持：

```text
/compact
```

它会把当前 `history + state` 压成 `【上下文压缩】`，保留 transcript 审计，不重绘旧屏幕，只追加一条系统提示。

### 自动 compact

模型上下文窗口在 [constant/llm/constant_llm.py](constant/llm/constant_llm.py) 中维护。后端使用模型 `max_tokens` 的 80% 作为安全上下文预算：

```python
CONTEXT_USAGE_RATIO = 0.8
```

当工具循环或下一轮请求接近该阈值时，后端会自动 compact，TUI 会追加类似“已自动压缩上下文”的系统提示。

## CLI 命令

`python run_agent.py` 的 REPL 支持：

| 命令 | 说明 |
|---|---|
| `/help` | 打印帮助 |
| `/tools` | 列出所有已注册工具 |
| `/skills` | 列出所有 Skill |
| `/history` | 查看当前会话 history |
| `/sessions` | 列出本地会话 |
| `/new` | 新建会话 |
| `/switch ID` | 切换会话 |
| `/clear` | 清空并删除当前 active session |
| `/ctx on\|off` | 开关 ContextBuilder |
| `/msg on\|off` | 开关每轮 messages dump |
| `/quit` | 退出 |

## 工具系统

默认 light 模式常见工具：

| 工具 | 说明 |
|---|---|
| `file_read` | 读取文件 |
| `file_write` | 写文件，配合 read-before-write 保护 |
| `bash` | 执行 shell 命令 |
| `bash_task` | 后台任务管理 |
| `bash_permission` | Bash 权限相关控制 |
| `todo` | 任务拆解与状态管理 |
| `skill` | 加载 Skill 指令 |
| `run_skill_script` | 执行 Skill 附带脚本 |
| `ask_user_question` | 工具循环中向用户提问 |
| `my_advanced_search` | Tavily / SerpApi 搜索源可用时执行 Web 搜索 |
| MCP 子工具 | 从 `mcp.json` 中展开 |

full 模式额外注册：

- `memory`
- `rag`

自定义工具参考 [tools/TOOL_SYSTEM_DESIGN.md](tools/TOOL_SYSTEM_DESIGN.md)。

## MCP

项目根目录的 [mcp.json](mcp.json) 当前包含：

| server | 说明 |
|---|---|
| `amap-maps` | 高德地图 MCP，需要 `AMAP_MAPS_API_KEY` |
| `playwright` | Playwright MCP |

启动时 `run_agent.py` 会读取 `mcp.json`，把每个 MCP server 的子工具展开注册。跳过 MCP 可用：

```bash
python run_agent.py --no-mcp
```

## Skills

Skill 是 “Prompt as Capability”：用 Markdown + YAML frontmatter 写能力说明，必要时引用脚本。

常用命令：

```text
/skills
```

相关文档：

- [skills/SKILLS_GUIDE.md](skills/SKILLS_GUIDE.md)

## 项目结构

```text
cb-agent/
├── agent/                 AgentSession、LLM 客户端、事件、transport gateway
├── context/               ContextBuilder 与 Markdown memory provider
├── core/                  Message 等核心结构
├── memory/                full 模式旧记忆/RAG/存储
├── tools/                 原生工具、MCP 工具包装、ToolRegistry
├── skills/                Skill 加载与执行
├── ui-tui/                Ink/React TUI
├── constant/llm/          模型能力和上下文窗口配置
├── note/                  技术报告
├── mcp.json               MCP server 配置
├── run_agent.py           主入口：CLI / JSON-RPC gateway
├── agent_run_basic.py     简化 CLI 入口
├── requirements.txt       light 依赖
├── requirements-full.txt  full 依赖
└── pyproject.toml
```

## 设计要点

### ContextBuilder：GSSC

Gather 顺序：

1. system instructions
2. 本地 session state
3. Markdown memory state / related
4. full memory state / related
5. full RAG
6. history
7. additional packets

之后按相关性、新近性、MMR、多级优先级和 token 预算筛选。结构化 prompt 固定为：

- `[Role & Policies]`
- `[Task]`
- `[State]`
- `[Evidence]`
- `[Context]`
- `[Output]`

详细见 [context/README.md](context/README.md)。

### 单轮 messages 与跨轮 history 分离

单轮工具循环里的 OpenAI `messages` 可以包含：

- `assistant.tool_calls`
- `role=tool`
- `tool_call_id`
- 完整 tool result

这些协议字段不会跨轮直接恢复。跨轮只保存普通 user/assistant 文本、`【工作记录】` 和 `【上下文压缩】`，避免 tool_call_id 污染下一轮协议。

### TUI transport

TUI 通过 stdio JSON-RPC 与 Python 后端通信：

- stdout：NDJSON 事件/响应
- stdin：RPC 请求
- stderr：后端日志，写到 `~/.cb-agent/logs/gateway-<timestamp>.log`

详细见 [note/Stage5a stdio JSON-RPC 网关技术报告.md](<note/Stage5a stdio JSON-RPC 网关技术报告.md>)。

## 开发与测试

Python：

```bash
python -m py_compile agent_run_basic.py run_agent.py context/builder.py context/markdown_memory.py
python test/test_context_builder.py
python test/test_session_renderer.py
python test/test_transport.py
```

TUI：

```bash
cd ui-tui
npm test
npm run build
```

其它专项测试：

```bash
python test/test_bash_tool.py
python test/test_work_context.py
python test/test_memory_operations.py
python test/test_rag_operations.py
```

`test_memory_operations.py` 和 `test_rag_operations.py` 属于 full 能力测试，可能需要 full 依赖和相应 env。

## 技术报告

`note/` 下记录了主要演进：

- [跨轮工作上下文技术报告.md](<note/跨轮工作上下文技术报告.md>)
- [多会话隔离与TUI切换技术报告.md](<note/多会话隔离与TUI切换技术报告.md>)
- [Compact上下文压缩命令技术报告.md](<note/Compact上下文压缩命令技术报告.md>)
- [轻量Markdown记忆系统技术报告.md](<note/轻量Markdown记忆系统技术报告.md>)
- [Stage5a stdio JSON-RPC 网关技术报告.md](<note/Stage5a stdio JSON-RPC 网关技术报告.md>)
- [Bash 权限弹窗走 UI 通道技术报告.md](<note/Bash 权限弹窗走 UI 通道技术报告.md>)
- [Bash 工具 UI 输出预览字段技术报告.md](<note/Bash 工具 UI 输出预览字段技术报告.md>)

## 常见问题

### 启动时报 `LLM_MODEL_ID` 找不到

检查 `.env` 中的 `LLM_MODEL_ID` 是否在 [constant/llm/constant_llm.py](constant/llm/constant_llm.py) 登记。

### TUI 启动后 Python 后端退出

查看日志：

```text
~/.cb-agent/logs/gateway-<timestamp>.log
```

也可以指定 Python：

```bash
CB_AGENT_PYTHON=/path/to/python npm start
```

### `/tools` 里没有 `memory` 和 `rag`

这是默认 light 模式的预期行为。要使用旧向量记忆/RAG：

```bash
pip install -e ".[full]"
python run_agent.py --memory-system full
```

### Web 搜索不可用

`my_advanced_search` 使用核心依赖里的 `requests` 直连 Tavily 或 SerpApi，不需要额外 SDK。配置任意一个 API Key 后重启即可：

```env
TAVILY_API_KEY=...
SERPAPI_API_KEY=...
```

对应 Python 包在 full 依赖中。

### full RAG/Memory 依赖太重

使用默认 light 模式即可。Markdown 记忆不需要 embedding、Qdrant、Neo4j 或本地模型。

## License

MIT
