"""工具列表查询工具。

让模型动态获取当前系统中所有可用工具的名称和描述，替代系统提示词中的
静态工具清单。减少 system prompt token 占用，同时保持工具发现能力。

使用全局唯一的 ToolRegistry 单例 (global_registry)，确保和实际注册的
工具完全一致。
"""

from __future__ import annotations

from typing import Any, Dict, List

from tools.tool import Tool, ToolParameter
from tools.toolRegistry import global_registry


class ListToolsTool(Tool):
    """查询当前系统中所有可用工具的列表和描述。"""

    def __init__(self) -> None:
        super().__init__(
            name="list_tools",
            description=(
                "获取当前系统中所有可用工具的完整列表和功能描述。"
                "在开始任何任务前应先调用此工具了解有哪些能力可用，"
                "而不是盲目猜测或假设某工具存在。"
                "返回格式：每行一条，格式为 '- tool_name: description'。"
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        # 无需参数，直接返回全部工具列表
        return []

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        """返回全局 ToolRegistry 中所有工具的格式化描述。"""
        desc = global_registry.get_tools_description()
        if not desc or desc == "暂无可用工具":
            return "（当前没有已注册的工具）"
        return desc
