"""工具注册表 - HelloAgents原生工具系统"""

import threading
from typing import Optional, Any, Callable
from .tool import Tool
from typing import List, Dict

class ToolRegistry:
    """
    工具注册表

    提供工具的注册、管理和执行功能。
    支持两种工具注册方式：
    1. Tool对象注册（推荐）
    2. 函数直接注册（简便）
    """


    def __init__(self):
        # MCP 工具改为后台连接后，注册动作可能和主对话线程读取 tool schema
        # 同时发生。这里用 RLock 保护字典读写，并在读取列表时先做快照，避免
        # “遍历过程中字典变化”的竞态。
        self._lock = threading.RLock()
        self._tools: dict[str, Tool] = {}
        self._functions: dict[str, dict[str, Any]] = {}

    def register_tool(self, tool: Tool):
        """
        注册Tool对象

        Args:
            tool: Tool实例
        """
        with self._lock:
            if tool.name in self._tools:
                print(f"警告：工具 '{tool.name}' 已存在，将被覆盖。")
            self._tools[tool.name] = tool
        print(f"工具 '{tool.name}' 已注册。")

    def register_function(self, name: str, description: str, func: Callable[[str], str]):
        """
        直接注册函数作为工具（简便方式）

        Args:
            name: 工具名称
            description: 工具描述
            func: 工具函数，接受字符串参数，返回字符串结果
        """
        with self._lock:
            if name in self._functions:
                print(f"警告：工具 '{name}' 已存在，将被覆盖。")
            self._functions[name] = {
                "description": description,
                "func": func
            }
        print(f"工具 '{name}' 已注册。")

    def unregister(self, name: str):
        """注销工具"""
        with self._lock:
            if name in self._tools:
                del self._tools[name]
                print(f"工具 '{name}' 已注销。")
            elif name in self._functions:
                del self._functions[name]
                print(f"工具 '{name}' 已注销。")
            else:
                print(f"警告：工具 '{name}' 不存在。")

    def get_tool(self, name: str) -> Optional[Tool]:
        """获取Tool对象"""
        with self._lock:
            return self._tools.get(name)

    def get_function(self, name: str) -> Optional[Callable]:
        """获取工具函数"""
        with self._lock:
            func_info = self._functions.get(name)
        return func_info["func"] if func_info else None

    def execute_tool(self, name: str, input_text: str) -> str:
        """
        执行工具

        Args:
            name: 工具名称
            input_text: 输入参数

        Returns:
            工具执行结果
        """
        # 优先查找Tool对象
        with self._lock:
            tool = self._tools.get(name)
            func_info = self._functions.get(name)

        if tool is not None:
            try:
                # 简化参数传递，直接传入字符串
                return tool.run({"input": input_text})
            except Exception as e:
                return f"错误：执行工具 '{name}' 时发生异常: {str(e)}"

        # 查找函数工具
        elif func_info is not None:
            func = func_info["func"]
            try:
                return func(input_text)
            except Exception as e:
                return f"错误：执行工具 '{name}' 时发生异常: {str(e)}"

        else:
            return f"错误：未找到名为 '{name}' 的工具。"
    
    def execute_tool(self, name: str, input_dict: dict[str, Any]) -> str:
        """
        执行工具

        Args:
            name: 工具名称
            input_dict: 输入参数

        Returns:
            工具执行结果
        """
        # 优先查找Tool对象
        with self._lock:
            tool = self._tools.get(name)
            func_info = self._functions.get(name)

        if tool is not None:
            try:
                # 直接传入字典参数
                return tool.run(input_dict)
            except Exception as e:
                return f"错误：执行工具 '{name}' 时发生异常: {str(e)}"

        # 查找函数工具
        elif func_info is not None:
            func = func_info["func"]
            try:
                return func(input_dict)
            except Exception as e:
                return f"错误：执行工具 '{name}' 时发生异常: {str(e)}"

        else:
            return f"错误：未找到名为 '{name}' 的工具。"

    def get_tools_description(self) -> str:
        """
        获取所有可用工具的格式化描述字符串

        Returns:
            工具描述字符串，用于构建提示词
        """
        descriptions = []

        with self._lock:
            tools = list(self._tools.values())
            functions = list(self._functions.items())

        # Tool对象描述
        for tool in tools:
            descriptions.append(f"- {tool.name}: {tool.description}")

        # 函数工具描述
        for name, info in functions:
            descriptions.append(f"- {name}: {info['description']}")

        return "\n".join(descriptions) if descriptions else "暂无可用工具"
    

    def get_tools_description_openai_schema(self) -> Optional[List[Dict]]:
        """
        获取所有可用工具的openai function calling格式列表

        Returns:
            工具列表，用于模型调用Function Calling
        """
        descriptions = []

        with self._lock:
            tools = list(self._tools.values())
            functions = list(self._functions.items())

        # Tool对象描述
        for tool in tools:
            descriptions.append(tool.to_openai_schema())

        # 函数工具描述
        # TODO: 处理函数参数的类型和必填性
        for name, info in functions:
            descriptions.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": info["description"],
                    "parameters": {
                        "type": "object",
                        "properties": {},
                        "required": []
                    },
                }
            })
        
        return descriptions if descriptions else []

    def list_tools(self) -> list[str]:
        """列出所有工具名称"""
        with self._lock:
            return list(self._tools.keys()) + list(self._functions.keys())

    def get_all_tools(self) -> list[Tool]:
        """获取所有Tool对象"""
        with self._lock:
            return list(self._tools.values())

    def clear(self):
        """清空所有工具"""
        with self._lock:
            self._tools.clear()
            self._functions.clear()
        print("所有工具已清空。")

# 全局工具注册表
global_registry = ToolRegistry()
