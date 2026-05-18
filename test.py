from tools.tool import Tool
from tools.toolRegistry import ToolRegistry
from tools.toolParameter import ToolParameter
from tools.tools.search import SearchTool
from tools.mcp_tools.mcptool import MCPTool
import json
# toolRegistry = ToolRegistry()
# toolRegistry.register_tool(SearchTool())
# print(toolRegistry.get_tools_description())
# print(json.dumps(toolRegistry.get_tools_description_openai_schema(), ensure_ascii=False, indent=2))

# 读取 JSON 文件
with open(r'cb-agent\mcp.json', 'r', encoding='utf-8') as f:
    mcp_site_file = json.load(f)

# {'mcpServers': {'amap-maps': {'command': 'npx', 'args': ['-y', '@amap/amap-maps-mcp-server'], 'env': {'AMAP_MAPS_API_KEY': 'your_api_key'}}}}

server_cfg = mcp_site_file['mcpServers']['amap-maps']
server_cmd = [server_cfg['command']] + server_cfg['args']
tool = MCPTool(server_command=server_cmd, env=server_cfg['env'])

result = tool.run({"action": "list_tools"})
print(result)
