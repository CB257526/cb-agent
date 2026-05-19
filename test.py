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

from tools.mcp_tools.mcptools_add import load_mcp_tools
tools = load_mcp_tools()
print(tools)
