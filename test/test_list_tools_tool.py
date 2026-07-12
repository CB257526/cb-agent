"""list_tools 工具的单元测试。

覆盖 ListToolsTool 在注册表中查询工具列表的基本功能和边界情况。

跑法：
    ../venv/python.exe test/test_list_tools_tool.py
    ../venv/python.exe -m unittest test.test_list_tools_tool -v
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.tool import Tool, ToolParameter
from tools.toolRegistry import ToolRegistry
from tools.tools.list_tools_tool import ListToolsTool


class DummyTool(Tool):
    def __init__(self, name: str = "dummy_tool") -> None:
        description = "dummy description" if name == "dummy_tool" else f"{name} description"
        super().__init__(name=name, description=description)

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        return "ok"


class ReconstructedTool(DummyTool):
    """模拟无法深拷贝、只能调用无参构造器的扩展工具。"""

    def __deepcopy__(self, _memo):
        raise TypeError("not deepcopyable")


class TestListToolsTool(unittest.TestCase):
    def test_uses_injected_registry(self) -> None:
        registry = ToolRegistry()
        registry.register_tool(DummyTool())

        output = ListToolsTool(registry).run({})

        self.assertIn("- dummy_tool: dummy description", output)
        self.assertNotIn("当前没有已注册的工具", output)


    def test_registry_outputs_are_sorted_by_name(self) -> None:
        first = ToolRegistry()
        first.register_tool(DummyTool("z_tool"))
        first.register_function("a_func", "a function", lambda _args: "ok")
        first.register_tool(DummyTool("m_tool"))

        second = ToolRegistry()
        second.register_tool(DummyTool("m_tool"))
        second.register_tool(DummyTool("z_tool"))
        second.register_function("a_func", "a function", lambda _args: "ok")

        self.assertEqual(first.list_tools(), ["a_func", "m_tool", "z_tool"])
        self.assertEqual([tool.name for tool in first.get_all_tools()], ["m_tool", "z_tool"])
        self.assertEqual(
            first.get_tools_description_openai_schema(),
            second.get_tools_description_openai_schema(),
        )
        schema_names = [
            item["function"]["name"]
            for item in first.get_tools_description_openai_schema()
        ]
        self.assertEqual(schema_names, ["a_func", "m_tool", "z_tool"])

    def test_clone_filtered_rebinds_list_tools_to_child_registry(self) -> None:
        registry = ToolRegistry()
        registry.register_tool(DummyTool("safe_tool"))
        registry.register_tool(DummyTool("blocked_tool"))
        registry.register_tool(ListToolsTool(registry))

        child = registry.clone_filtered(deny_names={"blocked_tool"})

        self.assertIn("safe_tool", child.list_tools())
        self.assertIn("list_tools", child.list_tools())
        self.assertNotIn("blocked_tool", child.list_tools())
        output = child.execute_tool("list_tools", {})
        self.assertIn("safe_tool", output)
        self.assertNotIn("blocked_tool", output)

    def test_clone_reconstruction_preserves_registered_name(self) -> None:
        registry = ToolRegistry()
        original = ReconstructedTool("custom_runtime_name")
        registry.register_tool(original)

        child = registry.clone_filtered(allow_names={"custom_runtime_name"})
        cloned = child.get_tool("custom_runtime_name")

        self.assertIsNotNone(cloned)
        self.assertEqual(cloned.name, original.name)
        self.assertEqual(cloned.description, original.description)


if __name__ == "__main__":
    unittest.main(verbosity=2)
