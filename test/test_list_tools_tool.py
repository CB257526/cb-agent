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
    def __init__(self) -> None:
        super().__init__(name="dummy_tool", description="dummy description")

    def get_parameters(self) -> List[ToolParameter]:
        return []

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        return "ok"


class TestListToolsTool(unittest.TestCase):
    def test_uses_injected_registry(self) -> None:
        registry = ToolRegistry()
        registry.register_tool(DummyTool())

        output = ListToolsTool(registry).run({})

        self.assertIn("- dummy_tool: dummy description", output)
        self.assertNotIn("当前没有已注册的工具", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
