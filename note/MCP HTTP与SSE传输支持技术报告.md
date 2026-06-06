# MCP HTTP 与 SSE 传输支持技术报告

> 对应本次更新：完善 cb-agent 的 MCP 加载系统，支持 `stdio`、`http`、`sse` 三类传输，补齐远端 MCP 配置解析、错误诊断、TUI 状态展示与回归测试。

---

## 背景

之前 cb-agent 的 MCP 加载逻辑主要面向本地 stdio server：

- `mcp.json` 只会被解析成 `server_command`。
- `MCPTool` 只保存命令数组，HTTP/SSE 这类远端配置会在包装层丢失。
- `MCPClient` 虽然有部分 HTTP/SSE 代码，但入口、配置字段和错误处理没有真正贯通。
- TUI `/mcp` 只能看到连接状态，看不出 server 使用的是 stdio、HTTP 还是 SSE。
- `${VAR}` 环境变量未展开时，会把字面量占位符发给远端服务，最终表现成难排查的 400/401。

这导致 GitHub Copilot MCP 这类远端 HTTP MCP 无法稳定接入，也很难判断失败是“transport 不支持”还是“鉴权配置有问题”。

---

## 顶层目标

本次改造围绕三个目标：

1. **传输类型完整**
   - 支持本地 `stdio`。
   - 支持远端 Streamable HTTP，即 `type: "http"` / `transport: "http"`。
   - 支持旧式 SSE，即 `transport: "sse"`。

2. **配置兼容性更强**
   - 兼容 Claude 风格 `mcpServers`。
   - 兼容 VS Code / GitHub 文档常见的 `servers`。
   - 兼容顶层 `headers` 和 `requestInit.headers`。
   - 支持 `type`、`transport`、`url`、`headers`、`auth`、`verify`、`sse_read_timeout`。

3. **错误可诊断**
   - `.env` 自动加载，保证独立诊断脚本也能展开 `${GITHUB_PAT}`。
   - 连接前拦截未展开的 `${VAR}`。
   - 单个 server 配错不阻塞其它 MCP server。
   - HTTP 400/401/403 显示 URL、短响应体和 GitHub MCP 专项提示。

---

## 关键改动

### 1. `tools/mcp_tools/mcptools_add.py`

该文件从“只读 stdio 命令数组”升级为 MCP 配置规范化入口。

新增能力：

- `_load_env_for_mcp_config()`
  - 自动加载 `mcp.json` 同目录下的 `.env`。
  - 使用 `override=False`，系统环境变量优先。
  - 解决直接运行诊断脚本时 `.env` 未加载导致 `${GITHUB_PAT}` 误判缺失的问题。

- `_normalize_transport()`
  - 从 `transport`、`type` 或 `url` 推断传输类型。
  - 把 `streamable-http`、`streamable_http` 等别名统一成 `http`。

- `_find_unresolved_env()` / `_raise_if_unresolved_env()`
  - 递归检查配置里残留的 `${VAR}`。
  - 如果变量缺失，在本地直接报：

```text
MCP server github 配置引用了未设置的环境变量: GITHUB_PAT。
请在 .env 或系统环境变量中补齐，或使用 ${VAR:-default} 提供默认值。
```

- `_build_stdio_config()`
  - 生成 stdio 配置。
  - 保留旧字段 `server_command`，兼容已有调用方。

- `_build_remote_config()`
  - 生成 HTTP/SSE 配置。
  - 保留 `headers`、`auth`、`verify`、`sse_read_timeout`。
  - 把 `requestInit.headers` 折叠到 FastMCP 需要的顶层 `headers`。

- `load_mcp_server_configs(..., collect_errors=True)`
  - 默认行为保持严格：配置错误会抛出异常。
  - 后台加载使用 `collect_errors=True`：单个 server 错误写入 `config_error`，其它 server 继续加载。

---

### 2. `tools/mcp_tools/client.py`

该文件被整理成统一 FastMCP 客户端适配层。

支持的输入源：

| 输入 | 行为 |
|---|---|
| `FastMCP` 实例 | 内存传输，主要用于测试 |
| `dict` 配置 | 按 `transport` 创建 stdio/http/sse transport |
| `https://...` URL | 默认 Streamable HTTP |
| `.py` 文件 | Python stdio |
| 命令数组 | 通用 stdio |

HTTP/SSE 相关实现：

- `StreamableHttpTransport(url=..., headers=..., auth=..., verify=...)`
- `SSETransport(url=..., headers=..., auth=..., verify=...)`

错误诊断增强：

- 连接前再次拒绝未展开 `${VAR}`，防止未来绕过 `mcptools_add.py` 的入口直接构造 `MCPClient(config)`。
- 捕获带 `response` 的 HTTP 异常，整理成中文错误。
- 对 `https://api.githubcopilot.com/mcp/` 的 400/401/403 增加专项 hint：

```text
hint=请确认 GITHUB_PAT 是有效 GitHub PAT，且账号/组织允许使用 GitHub Copilot MCP
```

这样日志里能直接看出问题在鉴权，而不是误以为 HTTP MCP transport 没接通。

---

### 3. `tools/mcp_tools/mcptool.py`

`MCPTool` 从只保存 `server_command` 改为保存完整 `server_config`。

主要变化：

- stdio/http/sse 统一通过 `_client_source()` 进入 `MCPClient`。
- 发现工具时支持 `strict_discovery=True`。
  - 后台加载路径使用严格模式，失败会进入 server 的 error 状态。
  - 手动构造仍保留旧兼容行为：记录错误但不中断对象创建。
- 外部 MCP server 统一使用持久化客户端，避免每次调用都重复握手。
- 删除客户端层 debug `print`，避免污染 JSON-RPC stdout。

---

### 4. `run_agent.py`

后台 MCP 加载流程增强：

- `_prepare_mcp_loading()` 使用 `load_mcp_server_configs(collect_errors=True)`。
- MCP 状态项新增公开字段 `transport`。
- 配置错误的 server 直接进入 `error` 状态，不发起网络连接。
- 一个 server 出错不影响其它 server 注册。
- 后台实例化 `MCPTool` 时传入完整 `server_config` 与 `strict_discovery=True`。

这让 `/mcp` 可以显示类似：

```text
github: error (transport=http, error=...)
playwright: connected (transport=stdio, tools=...)
```

---

### 5. TUI 状态展示

涉及文件：

- `ui-tui/src/types.ts`
- `ui-tui/src/commands.ts`
- `ui-tui/src/__tests__/mcpStatus.test.ts`

`MCPServerStatus` 增加：

```ts
transport?: "stdio" | "http" | "sse" | string;
```

`/mcp` 输出会显示：

```text
transport=http
transport=stdio
transport=sse
```

这对排查很重要，因为用户能第一眼确认配置是否走到了预期 transport。

---

## GitHub MCP 400 的定位结论

用户遇到的错误：

```text
MCP 服务器 github 连接失败: Client error '400 Bad Request' for url 'https://api.githubcopilot.com/mcp/'
```

排查结果：

1. `github` server 已经被识别为 `transport=http`。
2. `https://api.githubcopilot.com/mcp/` 已经由 FastMCP Streamable HTTP transport 连接。
3. `.env` 中的 `GITHUB_PAT` 已能被读取，不再是 `${GITHUB_PAT}` 字面量未展开。
4. 使用当前 `GITHUB_PAT` 请求 GitHub REST `/user` 返回：

```text
401 Bad credentials
```

因此当前 400 的主要原因不是 cb-agent 不支持 HTTP MCP，而是 `GITHUB_PAT` 无效、过期、复制错误，或账号/组织没有允许使用 GitHub Copilot MCP。

有效配置示例：

```json
{
  "mcpServers": {
    "github": {
      "type": "http",
      "url": "https://api.githubcopilot.com/mcp/",
      "headers": {
        "Authorization": "Bearer ${GITHUB_PAT}"
      }
    }
  }
}
```

也兼容 GitHub / VS Code 文档常见形状：

```json
{
  "servers": {
    "github": {
      "url": "https://api.githubcopilot.com/mcp/",
      "requestInit": {
        "headers": {
          "Authorization": "Bearer ${GITHUB_PAT}"
        }
      }
    }
  }
}
```

---

## 测试覆盖

新增测试文件：

```text
test/test_mcp_transports.py
ui-tui/src/__tests__/mcpStatus.test.ts
```

覆盖内容：

- 解析 stdio/http/sse 三类配置。
- stdio 缺少 `command` 时明确报错。
- 官方 `servers + requestInit.headers` 写法可解析。
- 未展开环境变量会在连接前失败。
- `collect_errors=True` 时坏 server 不影响其它 server。
- 直接构造 `MCPClient(config)` 时仍会拒绝未展开 `${VAR}`。
- 真实启动本地 FastMCP stdio server 并调用工具。
- 真实启动本地 FastMCP Streamable HTTP server 并调用工具。
- 真实启动本地 FastMCP SSE server 并调用工具。
- TUI `/mcp` 状态展示包含 `transport`。

---

## 验证命令

本次更新已验证：

```powershell
..\venv\python.exe -m py_compile tools\mcp_tools\client.py tools\mcp_tools\mcptool.py tools\mcp_tools\mcptools_add.py run_agent.py
..\venv\python.exe -m unittest discover -s test -p "test_mcp_transports.py"
..\venv\python.exe -m unittest discover -s test -p "test_transport.py"
cd ui-tui
npm test -- commands.test.ts mcpStatus.test.ts
npm run build
```

验证结果：

```text
test_mcp_transports.py: 9 tests OK
test_transport.py: 24 tests OK
commands.test.ts + mcpStatus.test.ts: 43 tests OK
ui-tui build: OK
```

---

## 后续建议

1. **GitHub token 检查**
   - 重新生成有效 GitHub PAT。
   - 写入项目 `.env`：

```text
GITHUB_PAT=ghp_xxx
```

   - 不要把真实 token 写入 `mcp.json`。

2. **OAuth 模式**
   - FastMCP 支持 `auth: "oauth"`，但 cb-agent 还没有做面向 TUI/CLI 的 OAuth 登录体验。
   - 如果后续希望像 VS Code 一样弹登录流程，需要单独设计 OAuth 授权、token 缓存和退出登录命令。

3. **MCP 健康检查命令**
   - 可以后续新增 `/mcp doctor`。
   - 自动检查 `.env`、token 是否能访问 GitHub REST API、远端 MCP 是否返回工具列表。

4. **文档同步**
   - README 的 MCP 小节后续可以进一步展开配置示例。
   - 当前技术报告先记录本次实现和排障结论。

