import json
from pathlib import Path
from typing import List

from .mcptool import MCPTool


def load_mcp_tools(mcp_json_path: str | None = None) -> List[MCPTool]:
    """从 mcp.json 读取 MCP 服务器配置，返回 MCPTool 列表。

    Args:
        mcp_json_path: mcp.json 的路径，默认为项目根目录下的 mcp.json

    Returns:
        MCPTool 实例列表
    """
    if mcp_json_path is None:
        mcp_json_path = Path(__file__).parent.parent.parent / "mcp.json"

    with open(mcp_json_path, "r", encoding="utf-8") as f:
        config = json.load(f)

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
