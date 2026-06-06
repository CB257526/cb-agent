import json
import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from .mcptool import MCPTool

logger = logging.getLogger(__name__)

# 匹配 ${VAR} 或 ${VAR:-default} 形式的占位符
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _load_env_for_mcp_config(mcp_json_path: Path) -> None:
    """读取 mcp.json 同目录下的 `.env`，让 MCP 配置解析可以独立使用。

    run_agent.py 启动时已经会加载项目 `.env`，但单测、诊断脚本或未来其它入口可能
    会直接调用 load_mcp_server_configs()。如果这里不补一次，`${GITHUB_PAT}` 这类
    占位符在独立解析时会误判为未设置。override=False 可以保护系统环境变量优先级，
    用户临时在 shell 里覆盖 token 时不会被文件里的旧值盖掉。
    """
    env_path = mcp_json_path.parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception as e:
        logger.warning("加载 MCP .env 失败: %s", e)


def _expand_env(value: Any) -> Any:
    """递归把字符串里的 ${VAR} / ${VAR:-default} 替换为环境变量值。

    - ${VAR}：未设置时保持原样并打 warning，由 MCP 子进程自行决定是否报错
    - ${VAR:-default}：未设置时使用 default
    - 非字符串原样返回；dict / list 递归处理
    """
    if isinstance(value, str):
        def _sub(m: re.Match) -> str:
            name = m.group(1)
            default = m.group(2)
            env_value = os.environ.get(name)
            if env_value:
                return env_value
            if default is not None:
                return default
            logger.warning("mcp.json 引用了未设置的环境变量: %s", name)
            return m.group(0)
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _find_unresolved_env(value: Any) -> List[str]:
    """递归找出展开后仍残留的环境变量占位符。

    HTTP/SSE MCP 的 header 会直接发给远端服务。如果 `${TOKEN}` 没有被展开，
    远端通常只会返回 400/401，用户很难从网络错误反推出是本地 `.env` 少了变量。
    因此这里在连接前做一次本地诊断，并把缺失变量名带到 MCP 状态里。
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


def _raise_if_unresolved_env(server_name: str, server_config: Dict[str, Any]) -> None:
    """配置进入连接层前必须已经没有 `${VAR}` 占位符。"""
    missing = _find_unresolved_env(server_config)
    if not missing:
        return
    variables = ", ".join(missing)
    raise ValueError(
        f"MCP server {server_name} 配置引用了未设置的环境变量: {variables}。"
        "请在 .env 或系统环境变量中补齐，或使用 ${VAR:-default} 提供默认值。"
    )


def _normalize_transport(server_config: Dict[str, Any]) -> str:
    """根据 mcp.json 配置推断并规范化 transport 名称。

    Claude/VS Code 生态里常见写法有 `type: "http"`、`transport: "sse"`，
    也有人只写 `url`。这里统一折叠成 cb-agent 内部使用的
    `stdio/http/sse` 三种值，避免后续分支散落同一套判断。
    """
    raw = str(
        server_config.get("transport")
        or server_config.get("type")
        or ("http" if server_config.get("url") else "stdio")
    ).strip().lower()
    aliases = {
        "streamable-http": "http",
        "streamable_http": "http",
        "streamablehttp": "http",
    }
    return aliases.get(raw, raw)


def _build_stdio_config(server_name: str, server_config: Dict[str, Any]) -> Dict[str, Any]:
    """生成 stdio MCP 配置。

    stdio server 需要 command，可选 args/env/cwd。这里仍保留旧字段
    `server_command`，让历史调用方无需改动也能继续工作。
    """
    command = str(server_config.get("command") or "").strip()
    args = server_config.get("args", [])
    if not command:
        raise ValueError(f"MCP server {server_name} 使用 stdio transport 时必须配置 command")
    if not isinstance(args, list):
        raise ValueError(f"MCP server {server_name} 的 args 必须是数组")
    return {
        "name": server_name,
        "transport": "stdio",
        "command": command,
        "args": args,
        "server_command": [command] + args,
        "env": server_config.get("env"),
        "cwd": server_config.get("cwd"),
    }


def _build_remote_config(server_name: str, server_config: Dict[str, Any], transport: str) -> Dict[str, Any]:
    """生成 HTTP/SSE MCP 配置。

    远端 MCP 不启动本地子进程，后端只需要 url 以及 headers/auth 等 HTTP
    连接参数。headers 里可能包含 `${TOKEN}`，在进入本函数前已经统一展开。
    """
    url = str(server_config.get("url") or "").strip()
    if not url:
        raise ValueError(f"MCP server {server_name} 使用 {transport} transport 时必须配置 url")
    request_init = server_config.get("requestInit") if isinstance(server_config.get("requestInit"), dict) else {}
    # GitHub 等官方文档在不同宿主里会出现 `headers` 和 `requestInit.headers`
    # 两种写法；FastMCP transport 只认顶层 headers，这里统一折叠。
    headers = server_config.get("headers")
    if headers is None and isinstance(request_init, dict):
        headers = request_init.get("headers")
    return {
        "name": server_name,
        "transport": transport,
        "url": url,
        "headers": headers,
        "auth": server_config.get("auth"),
        "verify": server_config.get("verify"),
        "sse_read_timeout": server_config.get("sse_read_timeout"),
    }


def load_mcp_server_configs(
    mcp_json_path: str | None = None,
    *,
    collect_errors: bool = False,
) -> List[Dict[str, Any]]:
    """从 mcp.json 读取 MCP 服务器配置，但不连接服务器。

    MCPTool 构造函数会同步发现远端工具，可能启动外部进程或等待网络连接。
    TUI 启动时只需要先知道“有哪些服务器要连”，所以把轻量配置读取单独拆出来，
    供后台加载器先渲染 pending/connecting 状态，再逐个真正实例化 MCPTool。

    collect_errors=True 时不会因为单个 server 配错而中断全部 MCP 加载；错误会作为
    `config_error` 写入该 server 的配置项，AgentRunner 再把它渲染为单 server error。
    """
    if mcp_json_path is None:
        mcp_json_path = Path(__file__).parent.parent.parent / "mcp.json"
    mcp_json_path = Path(mcp_json_path)
    _load_env_for_mcp_config(mcp_json_path)

    with open(mcp_json_path, "r", encoding="utf-8") as f:
        config: Dict[str, Any] = json.load(f)

    config = _expand_env(config)
    servers: List[Dict[str, Any]] = []
    # Claude / VS Code / GitHub 文档里外层键名并不完全一致：Claude 常用
    # `mcpServers`，VS Code/GitHub 示例常用 `servers`。优先使用 mcpServers；
    # 没有时降级到 servers，降低用户复制官方配置后的迁移成本。
    configured_servers = config.get("mcpServers")
    if configured_servers is None:
        configured_servers = config.get("servers", {})
    if not isinstance(configured_servers, dict):
        raise ValueError("mcp.json 的 mcpServers/servers 必须是对象")

    for server_name, server_config in configured_servers.items():
        try:
            if not isinstance(server_config, dict):
                raise ValueError(f"MCP server {server_name} 的配置必须是对象")

            transport = _normalize_transport(server_config)
            _raise_if_unresolved_env(server_name, server_config)
            if transport == "stdio":
                servers.append(_build_stdio_config(server_name, server_config))
            elif transport in {"http", "sse"}:
                servers.append(_build_remote_config(server_name, server_config, transport))
            else:
                raise ValueError(
                    f"MCP server {server_name} 使用了不支持的 transport: {transport}，"
                    "当前支持 stdio/http/sse"
                )
        except Exception as e:
            if not collect_errors:
                raise
            transport = _normalize_transport(server_config) if isinstance(server_config, dict) else "unknown"
            servers.append({
                "name": server_name,
                "transport": transport,
                "config_error": str(e),
            })
    return servers


def load_mcp_tools(mcp_json_path: str | None = None) -> List["MCPTool"]:
    """从 mcp.json 读取 MCP 服务器配置，返回 MCPTool 列表。

    支持在配置中使用 ${VAR} / ${VAR:-default} 占位符，运行时从环境变量读取。
    这样 mcp.json 可以放心提交到仓库，密钥统一进 .env。

    Args:
        mcp_json_path: mcp.json 的路径，默认为项目根目录下的 mcp.json

    Returns:
        MCPTool 实例列表
    """
    # 这里保持旧同步 API 的行为，但把 MCPTool 的 import 放到函数内部。
    # 新的后台加载路径只需要读取 mcp.json，如果顶层 import MCPTool，就会在
    # 轻量启动阶段提前触碰 fastmcp 等依赖；虽然不会连接 server，但仍会拖慢
    # 或破坏“只读配置、不启动 MCP”的承诺。
    from .mcptool import MCPTool

    tools: List[MCPTool] = []

    for server in load_mcp_server_configs(mcp_json_path):
        tool = MCPTool(
            name=server["name"],
            server_command=server.get("server_command"),
            server_config=server,
            env=server.get("env"),
        )
        tools.append(tool)

    return tools
