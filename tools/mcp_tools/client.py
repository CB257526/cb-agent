"""cb-agent 的 MCP 客户端适配层。

该模块只负责把 cb-agent/Claude 风格的 mcp.json 配置转换成 FastMCP Client
可识别的 transport，并提供统一的 list_tools / call_tool 等异步方法。

支持的传输：
- memory：直接传入 FastMCP 实例，主要用于单测。
- stdio：本地命令或 Python 脚本，例如 npx、python server.py。
- http：MCP Streamable HTTP，例如 GitHub Copilot MCP。
- sse：旧式 SSE MCP endpoint。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

try:
    from fastmcp import Client, FastMCP
    from fastmcp.client.transports import PythonStdioTransport, SSETransport, StreamableHttpTransport, StdioTransport

    FASTMCP_AVAILABLE = True
except ImportError:
    FASTMCP_AVAILABLE = False
    Client = None
    FastMCP = None
    PythonStdioTransport = None
    SSETransport = None
    StreamableHttpTransport = None
    StdioTransport = None


_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _normalize_transport(value: Any) -> str:
    """把不同生态里的 transport 名称规范成 FastMCP 需要的值。"""
    raw = str(value or "stdio").strip().lower()
    aliases = {
        "streamable-http": "http",
        "streamable_http": "http",
        "streamablehttp": "http",
    }
    return aliases.get(raw, raw)


def _without_none(values: Dict[str, Any]) -> Dict[str, Any]:
    """过滤 None，避免把未配置字段传给旧版本 FastMCP transport。"""
    return {key: value for key, value in values.items() if value is not None}


def _is_python_command(command: str) -> bool:
    """判断 command 是否是 Python 解释器。"""
    name = Path(command).name.lower()
    return name in {"python", "python.exe", "python3", "python3.exe"} or name.startswith("python")


def _find_unresolved_env(value: Any) -> List[str]:
    """找出仍残留在配置中的环境变量占位符。

    load_mcp_server_configs 会先做一次诊断；这里再放一层，是为了覆盖单测、
    手写 MCPClient(config) 或未来其它入口直接传完整配置的场景。
    """
    found: List[str] = []
    if isinstance(value, str):
        found.extend(match.group(1) for match in _ENV_PATTERN.finditer(value))
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_find_unresolved_env(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_find_unresolved_env(item))
    return sorted(set(found))


def _raise_if_unresolved_env(config: Dict[str, Any]) -> None:
    """连接前拒绝带 `${VAR}` 的配置，避免向远端发送无效 header。"""
    missing = _find_unresolved_env(config)
    if missing:
        variables = ", ".join(missing)
        name = str(config.get("name") or "unknown")
        raise ValueError(
            f"MCP server {name} 配置引用了未设置的环境变量: {variables}。"
            "请在 .env 或系统环境变量中补齐。"
        )


def _format_http_error(exc: Exception) -> str:
    """把 httpx/FastMCP 抛出的 HTTP 状态错误整理成适合日志展示的中文信息。"""
    response = getattr(exc, "response", None)
    if response is None:
        return str(exc)
    status_code = getattr(response, "status_code", None)
    request = getattr(response, "request", None)
    url = getattr(request, "url", None)
    try:
        body = getattr(response, "text", "") or ""
    except Exception:
        body = ""
    body = body.strip().replace("\r", " ").replace("\n", " ")
    if len(body) > 500:
        body = body[:500] + "..."
    parts = [f"HTTP {status_code}" if status_code else "HTTP 请求失败"]
    if url:
        parts.append(f"url={url}")
    if body:
        parts.append(f"response={body}")
    if url and "api.githubcopilot.com/mcp" in str(url) and status_code in {400, 401, 403}:
        parts.append("hint=请确认 GITHUB_PAT 是有效 GitHub PAT，且账号/组织允许使用 GitHub Copilot MCP")
    return "；".join(parts)


class MCPClient:
    """支持 memory / stdio / http / sse 的 MCP 客户端。

    server_source 可以是 FastMCP 实例、URL、命令数组或完整配置字典。配置字典来自
    mcptools_add.load_mcp_server_configs，字段会保留 headers/auth/verify 等 HTTP 参数。
    """

    def __init__(
        self,
        server_source: Union[str, List[str], Any, Dict[str, Any]],
        server_args: Optional[List[str]] = None,
        transport_type: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        **transport_kwargs: Any,
    ):
        if not FASTMCP_AVAILABLE:
            raise ImportError(
                "MCP client requires fastmcp>=2.0. Install it with: pip install fastmcp>=2.0"
            )

        self.server_args = server_args or []
        self.transport_type = _normalize_transport(transport_type) if transport_type else None
        self.env = env or {}
        self.transport_kwargs = transport_kwargs
        self.server_source = self._prepare_server_source(server_source)
        self.client: Optional[Client] = None  # type: ignore[valid-type]
        self._context_manager = None

    def _prepare_server_source(self, server_source: Union[str, List[str], Any, Dict[str, Any]]):
        """根据 server_source 类型创建 FastMCP transport 或直接返回可用对象。"""
        if FastMCP is not None and isinstance(server_source, FastMCP):
            return server_source

        if isinstance(server_source, dict):
            return self._create_transport_from_config(server_source)

        if isinstance(server_source, str) and server_source.startswith(("http://", "https://")):
            transport_type = self.transport_type or "http"
            if transport_type == "sse":
                return SSETransport(url=server_source, **self.transport_kwargs)
            return StreamableHttpTransport(url=server_source, **self.transport_kwargs)

        if isinstance(server_source, str) and server_source.endswith(".py"):
            return PythonStdioTransport(
                script_path=server_source,
                args=self.server_args,
                env=self.env or None,
                **self.transport_kwargs,
            )

        if isinstance(server_source, list) and server_source:
            command = str(server_source[0])
            args = [str(item) for item in server_source[1:]] + self.server_args
            return self._create_stdio_transport(command=command, args=args, env=self.env or None, cwd=None)

        return server_source

    def _create_transport_from_config(self, config: Dict[str, Any]):
        """从 mcp.json server 配置创建 transport。"""
        _raise_if_unresolved_env(config)
        transport_type = _normalize_transport(config.get("transport") or config.get("type") or ("http" if config.get("url") else "stdio"))

        if transport_type == "stdio":
            server_command = config.get("server_command")
            if server_command and not config.get("command"):
                command = str(server_command[0])
                args = [str(item) for item in server_command[1:]]
            else:
                command = str(config.get("command") or "")
                args = [str(item) for item in (config.get("args") or [])]
            if not command:
                raise ValueError("stdio MCP 配置缺少 command")
            return self._create_stdio_transport(
                command=command,
                args=args + self.server_args,
                env=config.get("env") or self.env or None,
                cwd=config.get("cwd"),
            )

        if transport_type in {"http", "sse"}:
            url = config.get("url")
            if not url:
                raise ValueError(f"{transport_type} MCP 配置缺少 url")
            options = _without_none({
                "url": url,
                "headers": config.get("headers"),
                "auth": config.get("auth"),
                "verify": config.get("verify"),
                "sse_read_timeout": config.get("sse_read_timeout"),
            })
            options.update(self.transport_kwargs)
            if transport_type == "sse":
                return SSETransport(**options)
            return StreamableHttpTransport(**options)

        raise ValueError(f"Unsupported MCP transport: {transport_type}")

    def _create_stdio_transport(
        self,
        command: str,
        args: List[str],
        env: Optional[Dict[str, str]],
        cwd: Optional[str],
    ):
        """创建 stdio transport，并对 python server.py 使用 PythonStdioTransport。"""
        # MCPTool 已经在外层持有长期 __aenter__ 上下文来保持会话；这里把
        # transport 自身的 keep_alive 默认关掉，避免临时发现工具后残留子进程或
        # 在事件循环关闭后触发 FastMCP 的析构噪声。显式传 keep_alive 时尊重调用方。
        transport_kwargs = {"keep_alive": False}
        transport_kwargs.update(self.transport_kwargs)
        if _is_python_command(command) and args and args[0].endswith(".py"):
            return PythonStdioTransport(
                script_path=args[0],
                args=args[1:],
                env=env,
                cwd=cwd,
                **transport_kwargs,
            )
        return StdioTransport(
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            **transport_kwargs,
        )

    async def __aenter__(self):
        self.client = Client(self.server_source)
        self._context_manager = self.client
        try:
            await self._context_manager.__aenter__()
        except Exception as exc:
            if getattr(exc, "response", None) is not None:
                raise RuntimeError(f"MCP HTTP 连接失败: {_format_http_error(exc)}") from exc
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._context_manager:
            await self._context_manager.__aexit__(exc_type, exc_val, exc_tb)
            self.client = None
            self._context_manager = None

    async def list_tools(self) -> List[Dict[str, Any]]:
        """列出 MCP server 提供的工具，并转成 cb-agent 包装层需要的 dict。"""
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.list_tools()
        tools = result.tools if hasattr(result, "tools") else result if isinstance(result, list) else []
        normalized = []
        for tool in tools:
            normalized.append({
                "name": getattr(tool, "name", ""),
                "description": getattr(tool, "description", "") or "",
                "input_schema": (
                    getattr(tool, "inputSchema", None)
                    or getattr(tool, "input_schema", None)
                    or {}
                ),
            })
        return normalized

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """调用 MCP 工具，并提取常见 text/data/blob 内容。"""
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")

        result = await self.client.call_tool(tool_name, arguments)
        if hasattr(result, "content") and result.content:
            values = [self._content_value(item) for item in result.content]
            return values[0] if len(values) == 1 else values
        if hasattr(result, "structured_content"):
            return result.structured_content
        return result

    async def list_resources(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")
        result = await self.client.list_resources()
        resources = result.resources if hasattr(result, "resources") else result if isinstance(result, list) else []
        return [
            {
                "uri": str(getattr(resource, "uri", "")),
                "name": getattr(resource, "name", "") or "",
                "description": getattr(resource, "description", "") or "",
                "mime_type": getattr(resource, "mimeType", None) or getattr(resource, "mime_type", None),
            }
            for resource in resources
        ]

    async def read_resource(self, uri: str) -> Any:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")
        result = await self.client.read_resource(uri)
        contents = result.contents if hasattr(result, "contents") else result if isinstance(result, list) else []
        values = [self._content_value(item) for item in contents]
        return values[0] if len(values) == 1 else values

    async def list_prompts(self) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")
        result = await self.client.list_prompts()
        prompts = result.prompts if hasattr(result, "prompts") else result if isinstance(result, list) else []
        return [
            {
                "name": getattr(prompt, "name", ""),
                "description": getattr(prompt, "description", "") or "",
                "arguments": getattr(prompt, "arguments", []),
            }
            for prompt in prompts
        ]

    async def get_prompt(self, prompt_name: str, arguments: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")
        result = await self.client.get_prompt(prompt_name, arguments or {})
        messages = result.messages if hasattr(result, "messages") else []
        return [
            {
                "role": getattr(message, "role", ""),
                "content": self._content_value(getattr(message, "content", "")),
            }
            for message in messages
        ]

    async def ping(self) -> bool:
        if not self.client:
            raise RuntimeError("Client not connected. Use 'async with client:' context manager.")
        try:
            await self.client.ping()
            return True
        except Exception:
            return False

    def get_transport_info(self) -> Dict[str, Any]:
        if not self.client:
            return {"status": "not_connected"}
        transport = getattr(self.client, "transport", None)
        if transport:
            return {
                "status": "connected",
                "transport_type": type(transport).__name__,
                "transport_info": str(transport),
            }
        return {"status": "unknown"}

    @staticmethod
    def _content_value(content: Any) -> Any:
        for attr in ("text", "data", "blob"):
            if hasattr(content, attr):
                return getattr(content, attr)
        return str(content)
