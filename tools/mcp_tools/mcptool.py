import asyncio
import concurrent.futures
import os
import threading
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastmcp import FastMCP

from tools.tool import Tool
from tools.toolParameter import ToolParameter

load_dotenv()


# 常见 stdio MCP server 的环境变量自动映射。显式 env / env_keys 的优先级更高；
# 这里仅用于兼容旧配置，避免用户每个 server 都手写 env。
MCP_SERVER_ENV_MAP = {
    "server-github": ["GITHUB_PERSONAL_ACCESS_TOKEN"],
    "server-slack": ["SLACK_BOT_TOKEN", "SLACK_TEAM_ID"],
    "server-google-drive": ["GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN"],
    "server-postgres": ["POSTGRES_CONNECTION_STRING"],
    "server-sqlite": [],
    "server-filesystem": [],
}


class MCPTool(Tool):
    """把一个 MCP server 包装成 cb-agent 原生 Tool。

    这个类同时服务两种场景：
    1. 非展开模式：模型调用一个通用 MCPTool，再传入 action/tool_name。
    2. 展开模式：启动时发现 server 的工具列表，再把每个 MCP 子工具包装成独立 Tool。

    旧实现只保存 `server_command`，因此 `mcp.json` 里的 HTTP/SSE 配置会在这里丢失。
    现在统一保存 `server_config`，stdio/http/sse 都经由同一个 MCPClient 入口连接。
    """

    def __init__(
        self,
        name: str = "mcp",
        description: Optional[str] = None,
        server_command: Optional[List[str]] = None,
        server_args: Optional[List[str]] = None,
        server: Optional[Any] = None,
        auto_expand: bool = True,
        env: Optional[Dict[str, str]] = None,
        env_keys: Optional[List[str]] = None,
        server_config: Optional[Dict[str, Any]] = None,
        strict_discovery: bool = False,
    ):
        self.server_config = dict(server_config or {})
        self.server_command = server_command or self.server_config.get("server_command")
        self.server_args = server_args or []
        self.server = server
        self._available_tools: List[Dict[str, Any]] = []
        self._discover_error: Optional[Exception] = None
        self.strict_discovery = strict_discovery
        self.auto_expand = auto_expand
        self.prefix = f"{name}_" if auto_expand else ""

        if self.server_command and not self.server_config:
            # 兼容历史调用方式：只传 server_command 时，内部补成统一 stdio 配置。
            self.server_config = {
                "transport": "stdio",
                "command": self.server_command[0],
                "args": self.server_command[1:],
                "server_command": self.server_command,
            }
        elif self.server_config.get("transport") == "stdio" and not self.server_command:
            command = self.server_config.get("command")
            args = self.server_config.get("args") or []
            if command:
                self.server_command = [command] + list(args)
                self.server_config["server_command"] = self.server_command

        self.env = self._prepare_env(env or self.server_config.get("env"), env_keys, self.server_command)
        if self.env and self.server_config:
            # stdio 会把 env 传给子进程；HTTP/SSE 通常依赖 headers/auth，但保留该字段无副作用。
            self.server_config["env"] = self.env

        self._persistent_loop: Optional[asyncio.AbstractEventLoop] = None
        self._persistent_client = None
        self._persistent_thread: Optional[threading.Thread] = None

        if not self.server_config and not self.server_command and server is None:
            self.server = self._create_builtin_server()

        self._discover_tools()

        if description is None:
            description = self._generate_description()

        super().__init__(name=name, description=description)

    def _prepare_env(
        self,
        env: Optional[Dict[str, str]],
        env_keys: Optional[List[str]],
        server_command: Optional[List[str]],
    ) -> Dict[str, str]:
        """准备传给 stdio MCP 子进程的环境变量。"""
        result_env: Dict[str, str] = {}

        if server_command:
            server_name = None
            for part in server_command:
                if "server-" in part:
                    server_name = part.split("/")[-1] if "/" in part else part
                    break
            if server_name and server_name in MCP_SERVER_ENV_MAP:
                for key in MCP_SERVER_ENV_MAP[server_name]:
                    value = os.getenv(key)
                    if value:
                        result_env[key] = value

        if env_keys:
            for key in env_keys:
                value = os.getenv(key)
                if value:
                    result_env[key] = value

        if env:
            result_env.update(env)

        return result_env

    def _create_builtin_server(self):
        """创建一个内存 MCP demo server，保留旧 API 的无参可用性。"""
        server = FastMCP("CB-Agent-BuiltinMCP")

        @server.tool()
        def add(a: float, b: float) -> float:
            """加法计算器"""
            return a + b

        @server.tool()
        def subtract(a: float, b: float) -> float:
            """减法计算器"""
            return a - b

        @server.tool()
        def multiply(a: float, b: float) -> float:
            """乘法计算器"""
            return a * b

        @server.tool()
        def divide(a: float, b: float) -> float:
            """除法计算器"""
            if b == 0:
                raise ValueError("除数不能为零")
            return a / b

        return server

    def _client_source(self):
        """返回 MCPClient 使用的统一连接源。

        优先级：内存 FastMCP server > 完整 server_config > 历史 server_command。
        HTTP/SSE 的 url、headers、auth、verify 等字段都靠 server_config 保留下来。
        """
        if self.server is not None:
            return self.server
        if self.server_config:
            return self.server_config
        return self.server_command

    def _is_external_server(self) -> bool:
        """是否需要持久化连接。

        内存 FastMCP server 可以临时连接；stdio/http/sse 都可能持有会话或昂贵握手，
        因此首次工具调用后保留同一个 MCPClient。
        """
        return self.server is None and self._client_source() is not None

    def _discover_tools(self):
        """连接 MCP server 并读取工具列表。

        Runner 后台加载使用 strict_discovery=True：发现失败直接抛出，状态显示 error。
        手动构造 MCPTool 时保留旧兼容行为：记录错误但不打断对象创建。
        """
        try:
            from tools.mcp_tools.client import MCPClient

            async def discover():
                async with MCPClient(self._client_source(), self.server_args, env=self.env) as client:
                    return await client.list_tools()

            try:
                asyncio.get_running_loop()

                def run_in_thread():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(discover())
                    finally:
                        new_loop.close()

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    self._available_tools = executor.submit(run_in_thread).result()
            except RuntimeError:
                self._available_tools = asyncio.run(discover())

        except Exception as e:
            self._discover_error = e
            self._available_tools = []
            if self.strict_discovery:
                raise

    def _generate_description(self) -> str:
        if not self._available_tools:
            return "连接到 MCP 服务器，调用工具、读取资源和获取提示词。支持 stdio、HTTP、SSE 和内存传输。"
        if self.auto_expand:
            return f"MCP 工具服务器，包含 {len(self._available_tools)} 个工具；这些工具会自动展开为独立工具。"

        desc_parts = [f"MCP 工具服务器，提供 {len(self._available_tools)} 个工具："]
        for tool in self._available_tools:
            tool_name = tool.get("name", "unknown")
            tool_desc = tool.get("description", "")
            desc_parts.append(f"- {tool_name}: {tool_desc}")
        desc_parts.append('调用格式：{"action": "call_tool", "tool_name": "工具名", "arguments": {...}}')
        return "\n".join(desc_parts)

    def get_expanded_tools(self) -> List[Tool]:
        """把 MCP 子工具展开为 cb-agent Tool。"""
        if not self.auto_expand:
            return []

        from .mcp_wrapper_tool import MCPWrappedTool

        return [
            MCPWrappedTool(mcp_tool=self, tool_info=tool_info, prefix=self.prefix)
            for tool_info in self._available_tools
        ]

    def _ensure_client(self):
        """确保外部 MCP server 的持久化客户端已连接。"""
        if self._persistent_client is not None:
            return
        if not self._is_external_server():
            return

        from tools.mcp_tools.client import MCPClient

        self._persistent_loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(self._persistent_loop)
            self._persistent_loop.run_forever()

        self._persistent_thread = threading.Thread(target=run_loop, daemon=True)
        self._persistent_thread.start()

        async def connect():
            client = MCPClient(self._client_source(), self.server_args, env=self.env)
            await client.__aenter__()
            return client

        future = asyncio.run_coroutine_threadsafe(connect(), self._persistent_loop)
        try:
            self._persistent_client = future.result(timeout=30)
        except Exception as e:
            if self._persistent_loop is not None:
                self._persistent_loop.call_soon_threadsafe(self._persistent_loop.stop)
            if self._persistent_thread is not None:
                self._persistent_thread.join(timeout=5)
            if self._persistent_loop is not None:
                self._persistent_loop.close()
            self._persistent_loop = None
            self._persistent_thread = None
            raise RuntimeError(f"MCP 持久化连接失败: {e}")

    def close(self):
        """关闭持久化 MCP 客户端连接。"""
        if self._persistent_client is None:
            return

        async def disconnect():
            try:
                await self._persistent_client.__aexit__(None, None, None)
            except Exception:
                pass

        try:
            if self._persistent_loop is not None:
                future = asyncio.run_coroutine_threadsafe(disconnect(), self._persistent_loop)
                future.result(timeout=10)
        except Exception:
            pass

        if self._persistent_loop is not None:
            self._persistent_loop.call_soon_threadsafe(self._persistent_loop.stop)
        if self._persistent_thread is not None:
            self._persistent_thread.join(timeout=5)
        if self._persistent_loop is not None:
            try:
                self._persistent_loop.close()
            except Exception:
                pass

        self._persistent_client = None
        self._persistent_loop = None
        self._persistent_thread = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    async def _execute_on_client(self, client, action: str, parameters: Dict[str, Any]) -> str:
        """在已连接的 MCPClient 上执行一次 MCP 操作。"""
        if action == "list_tools":
            tools = await client.list_tools()
            if not tools:
                return "没有找到可用的工具"
            lines = [f"找到 {len(tools)} 个工具:"]
            lines.extend(f"- {tool['name']}: {tool.get('description', '')}" for tool in tools)
            return "\n".join(lines)

        if action == "call_tool":
            tool_name = parameters.get("tool_name")
            arguments = parameters.get("arguments", {})
            if not tool_name:
                return "错误：必须指定 tool_name 参数"
            result = await client.call_tool(tool_name, arguments)
            return f"工具 '{tool_name}' 执行结果:\n{result}"

        if action == "list_resources":
            resources = await client.list_resources()
            if not resources:
                return "没有找到可用的资源"
            lines = [f"找到 {len(resources)} 个资源:"]
            lines.extend(f"- {item['uri']}: {item.get('name', '')}" for item in resources)
            return "\n".join(lines)

        if action == "read_resource":
            uri = parameters.get("uri")
            if not uri:
                return "错误：必须指定 uri 参数"
            content = await client.read_resource(uri)
            return f"资源 '{uri}' 内容:\n{content}"

        if action == "list_prompts":
            prompts = await client.list_prompts()
            if not prompts:
                return "没有找到可用的提示词"
            lines = [f"找到 {len(prompts)} 个提示词:"]
            lines.extend(f"- {item['name']}: {item.get('description', '')}" for item in prompts)
            return "\n".join(lines)

        if action == "get_prompt":
            prompt_name = parameters.get("prompt_name")
            prompt_arguments = parameters.get("prompt_arguments", {})
            if not prompt_name:
                return "错误：必须指定 prompt_name 参数"
            messages = await client.get_prompt(prompt_name, prompt_arguments)
            lines = [f"提示词 '{prompt_name}':"]
            lines.extend(f"[{item['role']}] {item['content']}" for item in messages)
            return "\n".join(lines)

        return f"错误：不支持的操作 '{action}'"

    def _run_via_persistent_client(self, action: str, parameters: Dict[str, Any]) -> str:
        async def run_op():
            return await self._execute_on_client(self._persistent_client, action, parameters)

        future = asyncio.run_coroutine_threadsafe(run_op(), self._persistent_loop)
        return future.result()

    def _run_via_temp_client(self, action: str, parameters: Dict[str, Any]) -> str:
        from tools.mcp_tools.client import MCPClient

        async def run_mcp_operation():
            async with MCPClient(self._client_source(), self.server_args, env=self.env) as client:
                return await self._execute_on_client(client, action, parameters)

        try:
            try:
                asyncio.get_running_loop()

                def run_in_thread():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        return new_loop.run_until_complete(run_mcp_operation())
                    finally:
                        new_loop.close()

                with concurrent.futures.ThreadPoolExecutor() as executor:
                    return executor.submit(run_in_thread).result()
            except RuntimeError:
                return asyncio.run(run_mcp_operation())
        except Exception as e:
            return f"MCP 异步操作失败: {e}"

    def run(self, parameters: Dict[str, Any]) -> str:
        """执行 MCP 操作。"""
        action = str(parameters.get("action", "")).lower()
        if not action and "tool_name" in parameters:
            action = "call_tool"
            parameters["action"] = action

        if not action:
            return "错误：必须指定 action 参数或 tool_name 参数"

        try:
            if self._is_external_server():
                self._ensure_client()
                if self._persistent_client is not None:
                    return self._run_via_persistent_client(action, parameters)
            return self._run_via_temp_client(action, parameters)
        except Exception as e:
            return f"MCP 操作失败: {e}"

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="操作类型: list_tools, call_tool, list_resources, read_resource, list_prompts, get_prompt",
                required=True,
            ),
            ToolParameter(
                name="tool_name",
                type="string",
                description="工具名称，call_tool 操作需要",
                required=False,
            ),
            ToolParameter(
                name="arguments",
                type="object",
                description="工具参数，call_tool 操作需要",
                required=False,
            ),
            ToolParameter(
                name="uri",
                type="string",
                description="资源 URI，read_resource 操作需要",
                required=False,
            ),
            ToolParameter(
                name="prompt_name",
                type="string",
                description="提示词名称，get_prompt 操作需要",
                required=False,
            ),
            ToolParameter(
                name="prompt_arguments",
                type="object",
                description="提示词参数，get_prompt 操作可选",
                required=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        valid_actions = {
            "list_tools",
            "call_tool",
            "list_resources",
            "read_resource",
            "list_prompts",
            "get_prompt",
        }
        action = str(parameters.get("action", "")).strip().lower()
        if not action:
            return "tool_name" in parameters
        return action in valid_actions
