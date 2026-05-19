# Tool 系统与 MCP 集成设计说明

## 目录

1. [架构总览](#架构总览)
2. [核心类详解](#核心类详解)
   - [Tool —— 工具抽象基类](#1-tool--工具抽象基类)
   - [ToolParameter —— 参数定义模型](#2-toolparameter--参数定义模型)
   - [ToolRegistry —— 工具注册表](#3-toolregistry--工具注册表)
   - [SearchTool —— 具体工具实现示例](#4-searchtool--具体工具实现示例)
3. [MCP 集成层](#mcp-集成层)
   - [MCPClient —— MCP 异步客户端](#5-mcpclient--mcp-异步客户端)
   - [MCPTool —— MCP 服务器适配器](#6-mcptool--mcp-服务器适配器)
   - [MCPWrappedTool —— 单个工具包装器](#7-mcpwrappedtool--单个工具包装器)
   - [load_mcp_tools —— 配置加载器](#8-load_mcp_tools--配置加载器)
4. [MCP 如何集成到 Tool 系统](#mcp-如何集成到-tool-系统)
5. [使用指南](#使用指南)
6. [设计优势](#设计优势)
7. [文件索引](#文件索引)

---

## 架构总览

```
                        ┌──────────────────────┐
                        │    ToolRegistry      │  ← 统一注册/查找/执行
                        │  (全局单例可选)        │
                        └──────┬───────────────┘
                               │ 管理
                ┌──────────────┼──────────────┐
                │              │              │
                ▼              ▼              ▼
        ┌──────────┐   ┌──────────┐   ┌──────────────┐
        │  Tool A  │   │  Tool B  │   │   MCPTool    │  ← 都继承 Tool
        │ (原生)    │   │ (原生)    │   │ (MCP适配器)   │
        └──────────┘   └──────────┘   └──────┬───────┘
                                             │
                              auto_expand=True 时展开
                                             │
                              ┌──────────────┼──────────────┐
                              │              │              │
                              ▼              ▼              ▼
                      ┌────────────┐ ┌────────────┐ ┌────────────┐
                      │MCPWrapped  │ │MCPWrapped  │ │MCPWrapped  │
                      │Tool: add   │ │Tool: greet │ │Tool: ...   │
                      └─────┬──────┘ └─────┬──────┘ └─────┬──────┘
                            │              │              │
                            └──────────────┼──────────────┘
                                           │ 委托调用
                                           ▼
                                  ┌────────────────┐
                                  │   MCPClient    │  ← 异步传输层
                                  │  (fastmcp 2.x) │
                                  └──────┬─────────┘
                                         │
                         ┌───────────────┼───────────────┐
                         │               │               │
                         ▼               ▼               ▼
                   ┌─────────┐   ┌──────────┐   ┌──────────┐
                   │ Memory   │   │  Stdio   │   │HTTP/SSE  │
                   │ (内置)    │   │ (子进程)  │   │ (远程)    │
                   └─────────┘   └──────────┘   └──────────┘
```

**核心思想：** 所有工具（无论是原生实现还是通过 MCP 协议从外部获取）最终都统一为 `Tool` 抽象，通过 `ToolRegistry` 进行管理。Agent 不需要关心工具来自哪里，只需要通过注册表查找和执行即可。

---

## 核心类详解

### 1. Tool —— 工具抽象基类

**文件：** `tools/tool.py`
**继承：** `abc.ABC`

所有工具的基类，定义了工具必须实现的契约。

#### 属性

| 属性 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 工具名称，全局唯一标识 |
| `description` | `str` | 工具描述，用于 LLM 理解工具用途 |

#### 方法

| 方法 | 签名 | 说明 |
|------|------|------|
| `run` | `(parameters: Dict[str, Any]) -> str` | **抽象方法**。执行工具逻辑，接收参数字典，返回字符串结果 |
| `get_parameters` | `() -> List[ToolParameter]` | **抽象方法**。返回工具的参数定义列表 |
| `to_openai_schema` | `() -> Dict[str, Any]` | 将工具转换为 OpenAI Function Calling 兼容的 JSON Schema 格式 |

#### `to_openai_schema()` 详述

这是连接 Tool 系统和 OpenAI 兼容 LLM 的关键方法。它将 `ToolParameter` 列表转换为：

```json
{
  "type": "function",
  "function": {
    "name": "<self.name>",
    "description": "<self.description>",
    "parameters": {
      "type": "object",
      "properties": {
        "<param.name>": {
          "type": "<param.type>",
          "description": "<param.description> (默认: <default>)"
        }
      },
      "required": ["<所有 required=True 的参数名>"]
    }
  }
}
```

对于 `type="array"` 的参数，会自动添加 `"items": {"type": "string"}`。

---

### 2. ToolParameter —— 参数定义模型

**文件：** `tools/toolParameter.py`
**继承：** `pydantic.BaseModel`

定义工具的单个输入参数。

#### 属性

| 属性 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `name` | `str` | 必填 | 参数名称 |
| `type` | `str` | 必填 | JSON Schema 类型（`"string"`, `"number"`, `"boolean"`, `"object"`, `"array"`） |
| `description` | `str` | 必填 | 参数描述，用于 LLM 理解参数用途 |
| `required` | `bool` | `True` | 是否为必填参数 |
| `default` | `Any` | `None` | 默认值（OpenAI Schema 不支持 `default` 字段，会拼接到描述中） |

#### 使用示例

```python
def get_parameters(self) -> List[ToolParameter]:
    return [
        ToolParameter(
            name="query",
            type="string",
            description="要搜索的关键词",
            required=True
        ),
        ToolParameter(
            name="max_results",
            type="number",
            description="最大返回结果数",
            required=False,
            default=5
        ),
    ]
```

---

### 3. ToolRegistry —— 工具注册表

**文件：** `tools/toolRegistry.py`

工具系统的管理中心，提供注册、查找、执行、导出等全部管理功能。

#### 内部存储

| 存储 | 类型 | 用途 |
|------|------|------|
| `_tools` | `Dict[str, Tool]` | 存储 `Tool` 对象，key 为工具名 |
| `_functions` | `Dict[str, Dict]` | 存储普通函数工具，value 为 `{"description": str, "func": Callable}` |

#### 方法列表

| 方法 | 签名 | 说明 |
|------|------|------|
| `register_tool` | `(tool: Tool) -> None` | 注册一个 `Tool` 对象。同名工具会被覆盖并打印警告 |
| `register_function` | `(name: str, description: str, func: Callable[[str], str]) -> None` | 用普通函数直接注册为工具（简便方式，无需继承 `Tool`） |
| `unregister` | `(name: str) -> None` | 注销指定名称的工具（同时检查 `_tools` 和 `_functions`） |
| `get_tool` | `(name: str) -> Optional[Tool]` | 按名称获取 `Tool` 对象 |
| `get_function` | `(name: str) -> Optional[Callable]` | 按名称获取注册的函数 |
| `execute_tool` | `(name: str, input_dict: Dict[str, Any]) -> str` | 执行工具。先查 `_tools`，再查 `_functions`。自动捕获异常并返回错误信息 |
| `get_tools_description` | `() -> str` | 获取所有工具的格式化描述文本（用于构造 LLM prompt） |
| `get_tools_description_openai_schema` | `() -> Optional[List[Dict]]` | 获取所有工具的 OpenAI Function Calling 格式列表（直接传给 OpenAI 兼容 API 的 `tools` 参数） |
| `list_tools` | `() -> List[str]` | 列出所有已注册工具的名称 |
| `get_all_tools` | `() -> List[Tool]` | 获取所有 `Tool` 对象（不包括纯函数工具） |
| `clear` | `() -> None` | 清空所有已注册的工具 |

#### 双模式注册

`ToolRegistry` 支持两种注册方式：

1. **Tool 对象注册（推荐）：** 适合复杂工具，有完整的参数定义、可导出 OpenAI Schema、可被 MCP 展开
2. **函数直接注册（简便）：** 适合简单工具，只需传入名称、描述和回调函数即可

```
register_tool(tool)     → _tools[name] = tool
register_function(...)  → _functions[name] = {"description": ..., "func": ...}
```

执行时的查找优先级：`_tools` > `_functions`

#### 全局单例

```python
# tools/toolRegistry.py 末尾
global_registry = ToolRegistry()
```

提供模块级全局注册表，方便跨模块共享。使用方式：

```python
from tools.toolRegistry import global_registry
global_registry.register_tool(my_tool)
```

---

### 4. SearchTool —— 具体工具实现示例

**文件：** `tools/tools/search.py`
**继承：** `Tool`

展示如何正确实现一个具体的 `Tool` 子类。

#### 构造函数 `__init__`

```python
def __init__(self):
    self.name = "my_advanced_search"
    self.description = "智能搜索工具，支持多个搜索源，自动选择最佳结果"
    self.search_sources = []
    self._setup_search_sources()  # 检测可用搜索源（Tavily / SerpApi）
```

#### `get_parameters()` 实现

```python
def get_parameters(self) -> List[ToolParameter]:
    return [
        ToolParameter(name="query", type="string", description="要搜索的关键词", required=True)
    ]
```

#### `run()` 实现

接收 `input_dict: Dict[str, Any]`，从中提取 `query`，按优先级尝试各个搜索源（Tavily → SerpApi），返回格式化的搜索结果。

#### 设计要点

- 构造函数中进行环境检测（API Key 可用性），优雅降级
- `run()` 方法有完整的错误处理和友好提示
- 支持多搜索源自动切换，对外暴露统一接口

---

## MCP 集成层

MCP（Model Context Protocol）是 Anthropic 提出的模型上下文协议。本项目通过 4 个文件将 MCP 服务器无缝集成到 Tool 系统中。

### 整体集成流程

```
mcp.json (配置)
    │
    ▼
load_mcp_tools()           ← 读取配置，创建 MCPTool 列表
    │
    ▼
MCPTool.__init__()         ← 连接 MCP 服务器，发现工具
    │
    ├── auto_expand=False  → 作为单个 Tool 使用，action 参数路由
    │
    └── auto_expand=True   → get_expanded_tools() 展开为 MCPWrappedTool 列表
                                  │
                                  ▼
                            ToolRegistry.register_tool()  逐个注册
                                  │
                                  ▼
                            Agent 像调用普通工具一样调用
```

---

### 5. MCPClient —— MCP 异步客户端

**文件：** `tools/mcp_tools/client.py`
**依赖：** `fastmcp >= 2.0.0`

底层传输层，封装了与 MCP 服务器的实际通信。设计为异步上下文管理器。

#### 支持的四种传输方式

| 传输类型 | 输入示例 | 使用场景 |
|----------|----------|----------|
| **Memory** | 直接传入 `FastMCP` 实例 | 测试、内置服务器 |
| **Stdio (Python)** | `"server.py"` 或 `["python", "server.py"]` | 本地 Python MCP 服务器子进程 |
| **Stdio (通用)** | `["npx", "-y", "@scope/server-name"]` | 本地 Node.js 或其他可执行文件 |
| **HTTP** | `"https://api.example.com/mcp"` | 远程 HTTP MCP 服务器 |
| **SSE** | `"https://api.example.com/mcp"` + `transport_type="sse"` | 远程 SSE 实时服务器 |

#### 构造函数

```python
def __init__(self,
             server_source: Union[str, List[str], FastMCP, Dict[str, Any]],
             server_args: Optional[List[str]] = None,
             transport_type: Optional[str] = None,
             env: Optional[Dict[str, str]] = None,
             **transport_kwargs)
```

`_prepare_server_source()` 方法自动识别 `server_source` 的类型并创建对应的传输对象：

1. `FastMCP` 实例 → 内存传输
2. `dict` → 从配置字典创建传输
3. `"http://..."` 或 `"https://..."` → HTTP/SSE 传输
4. `"*.py"` 字符串 → Python Stdio 传输
5. `list` → 命令 Stdio 传输

#### 方法列表

| 方法 | 签名 | 说明 |
|------|------|------|
| `list_tools` | `async () -> List[Dict]` | 列出 MCP 服务器提供的所有工具。返回包含 `name`, `description`, `input_schema` 的字典列表 |
| `call_tool` | `async (tool_name: str, arguments: Dict) -> Any` | 调用指定工具并返回解析后的结果。自动处理 `ToolResult` → 文本/数据的转换 |
| `list_resources` | `async () -> List[Dict]` | 列出 MCP 服务器暴露的资源（文件、数据等） |
| `read_resource` | `async (uri: str) -> Any` | 读取指定 URI 的资源内容 |
| `list_prompts` | `async () -> List[Dict]` | 列出 MCP 服务器提供的提示词模板 |
| `get_prompt` | `async (prompt_name: str, arguments: Dict) -> List[Dict]` | 获取提示词内容，返回 `[{"role": ..., "content": ...}]` 格式的消息列表 |
| `ping` | `async () -> bool` | 测试与 MCP 服务器的连接 |
| `get_transport_info` | `() -> Dict` | 获取当前传输层信息（连接状态、传输类型等） |

#### 使用方式

```python
# 必须通过 async with 使用
async with MCPClient(["npx", "-y", "server-name"]) as client:
    tools = await client.list_tools()
    result = await client.call_tool("tool_name", {"arg": "value"})
```

#### 结果解析逻辑

`call_tool()` 和 `read_resource()` 内部有智能的结果解析：
- 单条内容：直接提取 `.text` 或 `.data`
- 多条内容：返回提取后的列表
- 空内容：返回 `None`

---

### 6. MCPTool —— MCP 服务器适配器

**文件：** `tools/mcp_tools/mcptool.py`
**继承：** `Tool`

MCP 集成的核心类。它既是一个标准的 `Tool`（可直接注册到 `ToolRegistry`），又可以将其内部发现的所有 MCP 工具"展开"为独立的 `MCPWrappedTool` 对象。

#### 两种使用模式

**模式一：聚合模式（`auto_expand=False`）**

MCPTool 作为一个整体工具注册，通过 `action` 参数路由到不同操作：

```
Agent → MCPTool.run({"action": "call_tool", "tool_name": "xxx", "arguments": {...}})
```

**模式二：展开模式（`auto_expand=True`，默认）**

MCPTool 内部的每个 MCP 工具都展开为独立的 `Tool`，Agent 直接调用：

```
Agent → MCPWrappedTool("filesystem_read_file").run({"path": "/tmp/test.txt"})
```

#### 构造函数

```python
def __init__(self,
             name: str = "mcp",
             description: Optional[str] = None,
             server_command: Optional[List[str]] = None,
             server_args: Optional[List[str]] = None,
             server: Optional[Any] = None,       # FastMCP 实例
             auto_expand: bool = True,
             env: Optional[Dict[str, str]] = None,
             env_keys: Optional[List[str]] = None)
```

#### 环境变量处理

三级优先级，从高到低：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1（最高） | `env` 参数 | 直接在构造函数传入的环境变量字典 |
| 2 | `env_keys` 参数 | 从当前进程的 `os.environ` 中按 key 列表加载 |
| 3（最低） | 自动检测 | 根据 `server_command` 中的服务器名称，从 `MCP_SERVER_ENV_MAP` 查找需要的环境变量 |

`MCP_SERVER_ENV_MAP` 预置了常见 MCP 服务器的环境变量映射：

```python
MCP_SERVER_ENV_MAP = {
    "server-github":       ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "server-slack":        ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
    "server-google-drive": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
    "server-postgres":     ["POSTGRES_CONNECTION_STRING"],
    "server-sqlite":       [],
    "server-filesystem":   [],
}
```

#### 内置演示服务器

当不提供任何服务器参数时，自动创建一个包含 6 个工具的内置 FastMCP 服务器：

| 工具名 | 功能 |
|--------|------|
| `add` | 加法 |
| `subtract` | 减法 |
| `multiply` | 乘法 |
| `divide` | 除法（除零保护） |
| `greet` | 问候 |
| `get_system_info` | 获取系统信息 |

#### 方法列表

| 方法 | 签名 | 说明 |
|------|------|------|
| `run` | `(parameters: Dict[str, Any]) -> str` | 执行 MCP 操作。通过 `action` 参数路由到 6 种操作；如果未指定 `action` 但有 `tool_name`，自动推断为 `call_tool` |
| `get_parameters` | `() -> List[ToolParameter]` | 返回 MCP 工具的参数定义（`action`, `tool_name`, `arguments`, `uri`, `prompt_name`, `prompt_arguments`） |
| `get_expanded_tools` | `() -> List[Tool]` | **展开模式核心方法**。将发现的所有 MCP 工具包装为 `MCPWrappedTool` 列表 |
| `_discover_tools` | `() -> None` | 在 `__init__` 中调用，连接 MCP 服务器并获取工具列表 |
| `_prepare_env` | `(...) -> Dict[str, str]` | 按三级优先级合并环境变量 |
| `_create_builtin_server` | `() -> FastMCP` | 创建内置演示服务器 |
| `_generate_description` | `() -> str` | 根据是否展开模式自动生成工具描述 |

#### `run()` 支持的操作

| action 值 | 必需参数 | 说明 |
|-----------|----------|------|
| `list_tools` | 无 | 列出 MCP 服务器的全部工具 |
| `call_tool` | `tool_name`, `arguments` | 调用指定工具 |
| `list_resources` | 无 | 列出资源 |
| `read_resource` | `uri` | 读取资源内容 |
| `list_prompts` | 无 | 列出提示词模板 |
| `get_prompt` | `prompt_name` | 获取提示词内容 |

#### 异步执行策略

`run()` 方法智能处理异步执行环境：
- 如果没有运行中的事件循环 → `asyncio.run()`
- 如果已有运行中的事件循环 → 通过 `ThreadPoolExecutor` 在新线程中创建新事件循环执行

---

### 7. MCPWrappedTool —— 单个工具包装器

**文件：** `tools/mcp_tools/mcp_wrapper_tool.py`
**继承：** `Tool`

将 MCP 服务器上的一个工具包装为标准的 `Tool` 对象。这是"展开模式"的关键实现。

#### 核心原理

```
MCP 服务器的 input_schema (JSON Schema)
        │
        ▼
_parse_input_schema()  解析 properties 和 required
        │
        ▼
List[ToolParameter]    ← 标准的参数定义
        │
        ▼
run(params) → 委托给 MCPTool.run({"action": "call_tool", "tool_name": "...", "arguments": params})
```

#### 构造函数

```python
def __init__(self,
             mcp_tool: MCPTool,      # 父 MCPTool 引用
             tool_info: Dict,        # {"name": "...", "description": "...", "input_schema": {...}}
             prefix: str = "")       # 工具名前缀（如 "amap-maps_"）
```

工具名规则：`{prefix}{mcp_tool_name}`，例如 `amap-maps_geocode`

#### 方法

| 方法 | 说明 |
|------|------|
| `get_parameters()` | 返回从 MCP `input_schema` 解析的 `ToolParameter` 列表 |
| `run(params)` | 将参数封装为 MCP 调用格式，委托给父 `MCPTool.run()` |
| `_parse_input_schema(schema)` | 将 JSON Schema 的 `properties` 和 `required` 转换为 `ToolParameter` 列表 |

#### 调用链路

```
Agent
  → MCPWrappedTool.run({"path": "/tmp/test.txt"})
    → MCPTool.run({"action": "call_tool", "tool_name": "read_file", "arguments": {"path": "/tmp/test.txt"}})
      → MCPClient.call_tool("read_file", {"path": "/tmp/test.txt"})
        → MCP Server 执行
```

---

### 8. load_mcp_tools —— 配置加载器

**文件：** `tools/mcp_tools/mcptools_add.py`

从 `mcp.json` 配置文件批量创建 `MCPTool` 实例。

#### 函数签名

```python
def load_mcp_tools(mcp_json_path: str | None = None) -> List[MCPTool]
```

#### 工作流程

1. 读取 `mcp.json`（默认路径为项目根目录）
2. 遍历 `mcpServers` 中的每个服务器配置
3. 为每个服务器创建 `MCPTool` 实例
4. 返回 `MCPTool` 列表

#### mcp.json 配置格式

```json
{
  "mcpServers": {
    "server-name": {
      "command": "npx",
      "args": ["-y", "@scope/mcp-server-name"],
      "env": {
        "API_KEY": "xxx"
      }
    }
  }
}
```

每个服务器配置映射为：

```python
MCPTool(
    name="server-name",
    server_command=["npx", "-y", "@scope/mcp-server-name"],
    env={"API_KEY": "xxx"}
)
```

---

## MCP 如何集成到 Tool 系统

### 集成点总结

```
                    ┌────────────────────────────────────┐
                    │        MCP 协议 (外部服务器)          │
                    │  • 工具 (Tools)                     │
                    │  • 资源 (Resources)                 │
                    │  • 提示词 (Prompts)                 │
                    └──────────────┬─────────────────────┘
                                   │ fastmcp 库
                                   ▼
                    ┌────────────────────────────────────┐
                    │         MCPClient                   │
                    │  异步传输层（Memory/Stdio/HTTP/SSE）  │
                    └──────────────┬─────────────────────┘
                                   │
                    ┌──────────────▼─────────────────────┐
                    │          MCPTool                    │
                    │  继承 Tool，实现 run/get_parameters  │
                    │  作为 MCP 服务器的本地代理            │
                    └──────────────┬─────────────────────┘
                                   │
                    ┌──────────────▼─────────────────────┐
                    │       MCPWrappedTool               │
                    │  继承 Tool，每个 MCP 工具一个实例     │
                    │  将 JSON Schema → ToolParameter     │
                    └──────────────┬─────────────────────┘
                                   │ register_tool()
                                   ▼
                    ┌────────────────────────────────────┐
                    │         ToolRegistry               │
                    │  统一管理所有 Tool（原生 + MCP）      │
                    └──────────────┬─────────────────────┘
                                   │ to_openai_schema()
                                   ▼
                    ┌────────────────────────────────────┐
                    │         Agent / LLM                │
                    │  使用 Function Calling 调用工具      │
                    └────────────────────────────────────┘
```

### 关键设计决策

1. **统一抽象：** `MCPTool` 和 `MCPWrappedTool` 都继承 `Tool`，这意味着它们可以像原生工具一样被注册、查找、执行、导出 OpenAI Schema。Agent 不需要知道工具来自 MCP。

2. **两级粒度：** 聚合模式（一个 `MCPTool` = 整个 MCP 服务器）和展开模式（一个 `MCPWrappedTool` = 一个 MCP 工具）。展开模式（默认）对 LLM 更友好，因为每个工具都有独立的名称、描述和参数 schema。

3. **同步包裹异步：** MCP 协议本身是异步的（基于 `fastmcp`），但 `Tool.run()` 接口是同步的。`MCPTool` 内部通过 `asyncio.run()` 或线程池来处理这个转换，对调用者透明。

4. **配置驱动：** 通过 `mcp.json` 声明式管理 MCP 服务器，`load_mcp_tools()` 一键加载。

---

## 使用指南

### 场景一：创建并注册一个原生工具

```python
from tools.tool import Tool
from tools.toolParameter import ToolParameter
from tools.toolRegistry import ToolRegistry
from typing import Dict, Any, List

class WeatherTool(Tool):
    def __init__(self):
        super().__init__(
            name="get_weather",
            description="获取指定城市的天气信息"
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="city",
                type="string",
                description="城市名称",
                required=True
            )
        ]

    def run(self, parameters: Dict[str, Any]) -> str:
        city = parameters.get("city", "")
        # 实际的天气查询逻辑...
        return f"{city}的天气：晴，25°C"

# 注册到注册表
registry = ToolRegistry()
registry.register_tool(WeatherTool())

# 导出给 LLM 使用
schemas = registry.get_tools_description_openai_schema()
# schemas 可直接传给 OpenAI API 的 tools 参数
```

### 场景二：用函数快速注册简单工具

```python
from tools.toolRegistry import ToolRegistry

def reverse_text(input_text: str) -> str:
    return input_text[::-1]

registry = ToolRegistry()
registry.register_function(
    name="reverse",
    description="反转输入的文本",
    func=reverse_text
)

result = registry.execute_tool("reverse", {"input": "hello"})
# result = "olleh"
```

### 场景三：从 mcp.json 加载 MCP 工具（展开模式）

```python
from tools.mcp_tools.mcptools_add import load_mcp_tools
from tools.toolRegistry import ToolRegistry

registry = ToolRegistry()

# 加载所有 MCP 服务器
mcp_tools = load_mcp_tools()  # 读取项目根目录的 mcp.json

for mcp_tool in mcp_tools:
    if mcp_tool.auto_expand:
        # 展开模式：将每个 MCP 工具注册为独立工具
        for wrapped_tool in mcp_tool.get_expanded_tools():
            registry.register_tool(wrapped_tool)

# 现在所有 MCP 工具都可以通过 registry 统一调用
print(registry.list_tools())
# ['amap-maps_geocode', 'amap-maps_reverse_geocode', '12306-mcp_search_trains', ...]
```

### 场景四：使用 MCPTool 聚合模式

```python
from tools.mcp_tools.mcptool import MCPTool

tool = MCPTool(
    name="github",
    server_command=["npx", "-y", "@modelcontextprotocol/server-github"],
    env={"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_xxx"},
    auto_expand=False  # 聚合模式
)

# 列出工具
print(tool.run({"action": "list_tools"}))

# 调用工具
print(tool.run({
    "action": "call_tool",
    "tool_name": "search_repositories",
    "arguments": {"query": "machine learning"}
}))
```

### 场景五：直接使用 MCPClient 进行底层操作

```python
import asyncio
from tools.mcp_tools.client import MCPClient

async def main():
    async with MCPClient(["python", "my_mcp_server.py"]) as client:
        # 测试连接
        if await client.ping():
            print("连接成功")

        # 列出工具
        tools = await client.list_tools()
        for t in tools:
            print(f"  {t['name']}: {t['description']}")

        # 调用工具
        result = await client.call_tool("add", {"a": 3, "b": 4})
        print(f"3 + 4 = {result}")

        # 列出资源
        resources = await client.list_resources()
        for r in resources:
            content = await client.read_resource(r['uri'])
            print(f"{r['name']}: {content}")

asyncio.run(main())
```

### 场景六：完整 Agent 集成

```python
from tools.toolRegistry import ToolRegistry
from tools.mcp_tools.mcptools_add import load_mcp_tools
from agent.cb_agents import CbAgentsLLM

# 1. 准备工具注册表
registry = ToolRegistry()

# 2. 注册原生工具
registry.register_tool(SearchTool())
registry.register_tool(WeatherTool())

# 3. 加载并注册 MCP 工具
for mcp_tool in load_mcp_tools():
    for wrapped in mcp_tool.get_expanded_tools():
        registry.register_tool(wrapped)

# 4. 导出 OpenAI Schema
tool_schemas = registry.get_tools_description_openai_schema()

# 5. 传给 LLM
agent = CbAgentsLLM()
response = agent.think(
    user_input="帮我搜索北京的天气",
    tools=tool_schemas
)

# 6. 如果 LLM 返回了 tool_calls，执行并反馈
if response.tool_calls:
    for call in response.tool_calls:
        result = registry.execute_tool(
            call.function.name,
            call.function.arguments  # 注意：这里需要解析 JSON
        )
        # 将结果反馈给 LLM...
```

---

## 设计优势

### 1. 统一抽象，降低认知负担

无论是本地函数、HTTP API 还是 MCP 远程服务，对 Agent 来说都是"一个工具"。`Tool` 基类只定义了两个必须实现的抽象方法（`run` + `get_parameters`），新工具的学习成本极低。

### 2. 双模式注册，灵活性最大化

- **Tool 对象注册**：适合有复杂参数定义、需要导出 OpenAI Schema 的工具
- **函数直接注册**：适合简单场景，3 行代码即可完成

两种模式在同一个 `ToolRegistry` 中共存，`execute_tool()` 自动处理分发。

### 3. MCP 透明集成

MCP 工具通过 `MCPTool` 和 `MCPWrappedTool` 两层包装，完全融入 Tool 系统：
- MCP 的 `input_schema`（JSON Schema）自动转换为 `ToolParameter`
- MCP 的异步调用被包裹为同步接口
- MCP 工具可以和原生工具一样被注册、查找、导出 OpenAI Schema

### 4. 配置驱动，零代码接入

通过 `mcp.json` 声明式配置 MCP 服务器，`load_mcp_tools()` 一键加载。添加新的 MCP 服务只需修改 JSON 文件，无需写任何代码。

### 5. 智能环境变量管理

三级环境变量优先级（直接传递 > env_keys 加载 > 自动检测）+ 内置常见服务器映射表，避免了手动查找和传递环境变量的繁琐。

### 6. 多传输方式支持

`MCPClient` 支持 Memory、Stdio、HTTP、SSE 四种传输方式，覆盖了从本地测试到远程生产的所有场景：

| 场景 | 传输方式 |
|------|----------|
| 开发测试 | Memory（内置服务器） |
| 本地子进程 | Stdio（Python/Node.js 脚本） |
| 远程服务 | HTTP/SSE |
| CI/CD | Stdio + 配置字典 |

### 7. 展开模式对 LLM 友好

展开模式（默认）将每个 MCP 工具作为独立 Tool 注册到 `ToolRegistry`，每个工具有独立的名称、描述和参数 schema。这使得 LLM 的 Function Calling 可以精确匹配到具体的工具，而不需要通过一个聚合的 `action` 参数进行二次路由。

### 8. OpenAI 兼容

所有 `Tool` 子类都可以通过 `to_openai_schema()` 导出为标准的 OpenAI Function Calling 格式。`ToolRegistry.get_tools_description_openai_schema()` 一键获取全部工具的 schema，直接传给 OpenAI 兼容 API。

### 9. 异步透明化

MCP 协议本身是异步的，但通过 `MCPTool.run()` 内部的 `asyncio.run()` / 线程池处理，对调用者完全透明。Agent 调用 `registry.execute_tool()` 时不需要关心底层是同步还是异步。

### 10. 松耦合，高内聚

```
tools/
  tool.py              ← 抽象层（只依赖 toolParameter.py）
  toolParameter.py     ← 数据模型（只依赖 pydantic）
  toolRegistry.py      ← 管理层（只依赖 tool.py）
  tools/search.py      ← 实现层（依赖上面三层）
  mcp_tools/           ← MCP 集成层（依赖 Tool + fastmcp）
```

每一层职责清晰，依赖方向自上而下。替换任何一个层都不会影响其他层。

---

## 文件索引

| 文件 | 类/函数 | 职责 |
|------|---------|------|
| `tools/tool.py` | `Tool` | 工具抽象基类 |
| `tools/toolParameter.py` | `ToolParameter` | 参数定义数据模型 |
| `tools/toolRegistry.py` | `ToolRegistry`, `global_registry` | 工具注册/查找/执行/导出 |
| `tools/tools/search.py` | `SearchTool` | 具体工具实现示例 |
| `tools/mcp_tools/client.py` | `MCPClient` | MCP 异步传输客户端 |
| `tools/mcp_tools/mcptool.py` | `MCPTool` | MCP 服务器适配器 |
| `tools/mcp_tools/mcp_wrapper_tool.py` | `MCPWrappedTool` | 单工具包装器 |
| `tools/mcp_tools/mcptools_add.py` | `load_mcp_tools()` | mcp.json 加载器 |
| `mcp.json` | - | MCP 服务器配置文件 |
