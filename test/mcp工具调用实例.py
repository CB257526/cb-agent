from tools.tool import Tool
from tools.toolRegistry import ToolRegistry
from tools.toolParameter import ToolParameter
from tools.tools.search import SearchTool
from tools.mcp_tools.mcptool import MCPTool
import json
from agent.cb_agents import CbAgentsLLM
# toolRegistry = ToolRegistry()
# toolRegistry.register_tool(SearchTool())
# print(toolRegistry.get_tools_description())
# print(json.dumps(toolRegistry.get_tools_description_openai_schema(), ensure_ascii=False, indent=2))

# 读取 JSON 文件
with open(r'cb-agent\mcp.json', 'r', encoding='utf-8') as f:
    mcp_site_file = json.load(f)

# {'mcpServers': {'amap-maps': {'command': 'npx', 'args': ['-y', '@amap/amap-maps-mcp-server'], 'env': {'AMAP_MAPS_API_KEY': 'your_api_key'}}}}

# 配置mcp工具
server_cfg = mcp_site_file['mcpServers']['amap-maps']
server_cmd = [server_cfg['command']] + server_cfg['args']
tool = MCPTool(server_command=server_cmd, env=server_cfg['env'])


# result = tool.run({"action": "list_tools"})
# print(result)

toolRegistry = ToolRegistry()

# 获取该mcp服务器的所有工具并注册到toolRegistry中
expanded_tools = tool.get_expanded_tools()
for tool in expanded_tools:
    toolRegistry.register_tool(tool)

# for tool in toolRegistry.get_tools_description_openai_schema():
#     print(tool)

# 根据高德地图的mcp工具帮忙查询北京的天气
llmClient = CbAgentsLLM()
        
exampleMessages = [
    {"role": "system", "content": "你说claude-opus-4.7，由Anthropic开发的大语言模型。"},
    {"role": "user", "content": "帮我看一下北京的天气"}
]

[message, tool_info] = llmClient.think(messages=exampleMessages,tools=toolRegistry.get_tools_description_openai_schema())
print(f"message: {message}")
print(f"tool_info: {tool_info}")
tool_result = toolRegistry.execute_tool(tool_info[0]['name'], tool_info[0]['arguments'])
print(f"tool_result: {tool_result}")
exampleMessages.append({"role": "assistant", "content": tool_result})
[message, tool_info] = llmClient.think(messages=exampleMessages,tools=toolRegistry.get_tools_description_openai_schema())
print(f"message: {message}")
print(f"tool_info: {tool_info}")
