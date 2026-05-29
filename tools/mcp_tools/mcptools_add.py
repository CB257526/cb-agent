import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List

from .mcptool import MCPTool

logger = logging.getLogger(__name__)

# 匹配 ${VAR} 或 ${VAR:-default} 形式的占位符
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


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
            if name in os.environ:
                return os.environ[name]
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


def load_mcp_tools(mcp_json_path: str | None = None) -> List[MCPTool]:
    """从 mcp.json 读取 MCP 服务器配置，返回 MCPTool 列表。

    支持在配置中使用 ${VAR} / ${VAR:-default} 占位符，运行时从环境变量读取。
    这样 mcp.json 可以放心提交到仓库，密钥统一进 .env。

    Args:
        mcp_json_path: mcp.json 的路径，默认为项目根目录下的 mcp.json

    Returns:
        MCPTool 实例列表
    """
    if mcp_json_path is None:
        mcp_json_path = Path(__file__).parent.parent.parent / "mcp.json"

    with open(mcp_json_path, "r", encoding="utf-8") as f:
        config: Dict[str, Any] = json.load(f)

    config = _expand_env(config)

    tools: List[MCPTool] = []

    for server_name, server_config in config.get("mcpServers", {}).items():
        command = server_config.get("command", "")
        args = server_config.get("args", [])
        env = server_config.get("env")

        tool = MCPTool(
            name=server_name,
            server_command=[command] + args,
            env=env,
        )
        tools.append(tool)

    return tools
