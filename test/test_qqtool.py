"""qqtool 平台专用工具测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.qq.action_bridge import QQActionBridge, global_qq_action_bridge
from agent.qq.config import QQConfig
from tools.toolRegistry import ToolRegistry
from tools.tools.qq.functions import run_qq_function
from tools.tools.qqtool import QQTool


class TestQQTool(unittest.TestCase):
    def test_tool_schema_accepts_funname_and_args(self) -> None:
        tool = QQTool()
        params = {item.name: item for item in tool.get_parameters()}
        self.assertIn("funname", params)
        self.assertIn("args", params)
        self.assertTrue(tool.validate_parameters({"funname": "get_login_info", "args": {}}))
        self.assertFalse(tool.validate_parameters({"funname": "", "args": {}}))
        self.assertFalse(tool.validate_parameters({"funname": "get_login_info", "args": []}))

    def test_unconnected_bridge_returns_clear_error(self) -> None:
        tool = QQTool()
        payload = json.loads(tool.run({"funname": "get_login_info", "args": {}}))
        self.assertFalse(payload["ok"])
        self.assertIn("NapCat websocket is not connected", payload["error"])

    def test_action_payload_is_sent_through_bridge(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            return {"status": "ok", "retcode": 0, "data": [{"group_id": 1}, {"group_id": 2}]}

        async def run() -> None:
            token = bridge.register(asyncio.get_running_loop(), caller)
            try:
                with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge):
                    result = await asyncio.to_thread(
                        run_qq_function,
                        "get_group_list",
                        {},
                    )
            finally:
                bridge.unregister(token)
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "get_group_list")
            self.assertEqual(result["data"]["total"], 2)

        asyncio.run(run())
        self.assertEqual(calls, [("get_group_list", {})])

    def test_file_upload_uses_mapped_path_delivery(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            return {"status": "ok", "retcode": 0, "data": {"message": "ok"}}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "report.txt"
            shared = root / "outbound"
            source.write_text("hello", encoding="utf-8")

            async def run() -> None:
                token = bridge.register(asyncio.get_running_loop(), caller)
                try:
                    with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge), patch(
                        "tools.tools.qq.media.global_qq_action_bridge", bridge
                    ), patch.dict(
                        "os.environ",
                        {
                            "QQ_FILE_DELIVERY_MODE": "mapped_path",
                            "QQ_FILE_HOST_PREFIX": str(shared),
                            "QQ_FILE_NAPCAT_PREFIX": "/app/cb-agent-outbound",
                        },
                        clear=False,
                    ):
                        result = await asyncio.to_thread(
                            run_qq_function,
                            "upload_group_file",
                            {"group_id": "100", "file": str(source), "name": "report.txt"},
                        )
                finally:
                    bridge.unregister(token)
                self.assertTrue(result["ok"])
                self.assertEqual(result["metadata"]["file_delivery"]["delivery_method"], "mapped_path")

            asyncio.run(run())
            self.assertEqual(calls[0][0], "__cbagent_prepare_resource_reference__")
            self.assertEqual(calls[1][0], "upload_group_file")
            self.assertTrue(str(calls[1][1]["file"]).startswith("/app/cb-agent-outbound/"))
            self.assertEqual(len(list(shared.glob("report-*.txt"))), 1)

    def test_registry_can_register_qqtool_without_send_message_asset(self) -> None:
        registry = ToolRegistry()
        registry.register_tool(QQTool())
        self.assertIn("qqtool", registry.list_tools())
        self.assertNotIn("send_message_asset", registry.list_tools())


class TestQQAdapterBridge(unittest.TestCase):
    def test_adapter_prepare_resource_internal_action(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self, config: QQConfig) -> None:
                self.config = config

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "a.txt"
            shared = root / "shared"
            source.write_text("body", encoding="utf-8")
            adapter = DummyAdapter(QQConfig(
                file_delivery_mode="mapped_path",
                file_host_prefix=str(shared),
                file_napcat_prefix="/app/outbound",
            ))

            async def run() -> dict:
                return await adapter.call_action("__cbagent_prepare_resource_reference__", {"path": str(source)})

            result = asyncio.run(run())
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["data"]["ref"].startswith("/app/outbound/"))


if __name__ == "__main__":
    unittest.main()
