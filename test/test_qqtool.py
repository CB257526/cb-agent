"""qqtool 平台专用工具测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.platforms.context import reset_current_platform_conversation, set_current_platform_conversation
from agent.platforms.messages import ConversationKey
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
        self.assertTrue(tool.validate_parameters({"funname": "send_group_msg", "args": "{\"group_id\":\"100\",\"message\":\"hi\"}"}))
        self.assertFalse(tool.validate_parameters({"funname": "", "args": {}}))
        self.assertFalse(tool.validate_parameters({"funname": "get_login_info", "args": []}))

    def test_run_auto_parses_json_string_args(self) -> None:
        tool = QQTool()

        def fake_run(funname, args):  # noqa: ANN001
            return {"ok": True, "funname": funname, "action": "send_group_msg", "params": args, "duration_ms": 0}

        with patch("tools.tools.qqtool.run_qq_function", fake_run):
            payload = json.loads(tool.run({
                "funname": "send_group_msg",
                "args": "{\"group_id\":\"100\",\"message\":\"hi\"}",
            }))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["params"], {"group_id": "100", "message": "hi"})
        self.assertTrue(payload["metadata"]["args_auto_parsed"])

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

    def test_message_image_segment_uses_delivery_layer(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            if action == "__cbagent_prepare_resource_reference__":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "ref": "/app/cb-agent-outbound/a.png",
                        "metadata": {"delivery_method": "mapped_path"},
                    },
                }
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "a.png"
            source.write_bytes(b"png")

            async def run() -> None:
                token = bridge.register(asyncio.get_running_loop(), caller)
                try:
                    with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge), patch(
                        "tools.tools.qq.media.global_qq_action_bridge", bridge
                    ):
                        result = await asyncio.to_thread(
                            run_qq_function,
                            "send_group_msg",
                            {
                                "group_id": "100",
                                "message": [{"type": "image", "data": {"file": str(source)}}],
                            },
                        )
                finally:
                    bridge.unregister(token)
                self.assertTrue(result["ok"])
                self.assertEqual(result["metadata"]["message_file_delivery"][0]["delivery_method"], "mapped_path")

            asyncio.run(run())

        self.assertEqual(calls[0], ("__cbagent_prepare_resource_reference__", {"path": str(source)}))
        self.assertEqual(calls[1][0], "send_group_msg")
        self.assertEqual(calls[1][1]["message"][0]["data"]["file"], "/app/cb-agent-outbound/a.png")

    def test_prepared_mapped_path_is_not_delivered_twice(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        async def run() -> None:
            token = bridge.register(asyncio.get_running_loop(), caller)
            try:
                with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge), patch(
                    "tools.tools.qq.media.global_qq_action_bridge", bridge
                ), patch.dict(
                    "os.environ",
                    {"QQ_FILE_NAPCAT_PREFIX": "/app/cb-agent-outbound"},
                    clear=False,
                ):
                    result = await asyncio.to_thread(
                        run_qq_function,
                        "send_group_msg",
                        {
                            "group_id": "100",
                            "message": [
                                {
                                    "type": "image",
                                    "data": {"file": "/app/cb-agent-outbound/a.png"},
                                }
                            ],
                        },
                    )
            finally:
                bridge.unregister(token)
            self.assertTrue(result["ok"])
            self.assertEqual(
                result["metadata"]["message_file_delivery"][0]["delivery_method"],
                "prepared_mapped_path",
            )

        asyncio.run(run())

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "send_group_msg")
        self.assertEqual(calls[0][1]["message"][0]["data"]["file"], "/app/cb-agent-outbound/a.png")

    def test_cq_image_string_uses_delivery_layer(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            if action == "__cbagent_prepare_resource_reference__":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "ref": "/app/cb-agent-outbound/a.png",
                        "metadata": {"delivery_method": "mapped_path"},
                    },
                }
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "a.png"
            source.write_bytes(b"png")

            async def run() -> None:
                token = bridge.register(asyncio.get_running_loop(), caller)
                try:
                    with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge), patch(
                        "tools.tools.qq.media.global_qq_action_bridge", bridge
                    ):
                        result = await asyncio.to_thread(
                            run_qq_function,
                            "send_group_msg",
                            {
                                "group_id": "100",
                                "message": f"[CQ:image,file={source}]",
                            },
                        )
                finally:
                    bridge.unregister(token)
                self.assertTrue(result["ok"])
                self.assertEqual(result["metadata"]["message_file_delivery"][0]["delivery_method"], "mapped_path")

            asyncio.run(run())

        self.assertEqual(calls[0], ("__cbagent_prepare_resource_reference__", {"path": str(source)}))
        self.assertEqual(calls[1][0], "send_group_msg")
        self.assertEqual(calls[1][1]["message"], "[CQ:image,file=/app/cb-agent-outbound/a.png]")

    def test_file_upload_retries_next_delivery_candidate_on_napcat_failure(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            if action == "__cbagent_prepare_resource_reference__":
                return {
                    "status": "ok",
                    "retcode": 0,
                    "data": {
                        "ref": "/app/cb-agent-outbound/report.txt",
                        "metadata": {
                            "delivery_method": "mapped_path",
                            "delivery_note": "mapped",
                            "source_path": "report.txt",
                            "size": 5,
                            "errors": [],
                            "_delivery_candidates": [
                                {
                                    "method": "mapped_path",
                                    "ref": "/app/cb-agent-outbound/report.txt",
                                    "source_path": "report.txt",
                                    "size": 5,
                                    "note": "mapped",
                                },
                                {
                                    "method": "base64",
                                    "ref": "base64://aGVsbG8=",
                                    "source_path": "report.txt",
                                    "size": 5,
                                    "note": "inline",
                                },
                            ],
                        },
                    },
                }
            if action == "upload_group_file" and params["file"].startswith("/app/"):
                return {"status": "failed", "retcode": 1404, "wording": "file not found"}
            return {"status": "ok", "retcode": 0, "data": {"message": "ok"}}

        async def run() -> dict:
            token = bridge.register(asyncio.get_running_loop(), caller)
            try:
                with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge), patch(
                    "tools.tools.qq.media.global_qq_action_bridge", bridge
                ):
                    return await asyncio.to_thread(
                        run_qq_function,
                        "upload_group_file",
                        {"group_id": "100", "file": "report.txt", "name": "report.txt"},
                    )
            finally:
                bridge.unregister(token)

        result = asyncio.run(run())

        self.assertTrue(result["ok"])
        self.assertEqual(calls[1], (
            "upload_group_file",
            {"group_id": "100", "file": "/app/cb-agent-outbound/report.txt", "name": "report.txt"},
        ))
        self.assertEqual(calls[2][0], "upload_group_file")
        self.assertEqual(calls[2][1]["file"], "base64://aGVsbG8=")
        self.assertEqual(result["params"]["file"], "base64://...(17 chars)")
        self.assertEqual(result["metadata"]["file_delivery"]["delivery_method"], "base64")
        self.assertEqual(result["metadata"]["file_delivery"]["candidate_count"], 2)
        self.assertEqual(result["metadata"]["delivery_attempt_failures"][0]["methods"], ["mapped_path"])
        self.assertNotIn("_delivery_candidates", json.dumps(result["metadata"], ensure_ascii=False))

    def test_message_segment_base64_is_redacted_recursively(self) -> None:
        bridge = QQActionBridge()

        async def caller(action, params):  # noqa: ANN001
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        async def run() -> dict:
            token = bridge.register(asyncio.get_running_loop(), caller)
            try:
                with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge):
                    return await asyncio.to_thread(
                        run_qq_function,
                        "send_group_msg",
                        {
                            "group_id": "100",
                            "message": [
                                {
                                    "type": "image",
                                    "data": {"file": "base64://aGVsbG8="},
                                },
                                {
                                    "type": "text",
                                    "data": {"text": "done"},
                                },
                            ],
                        },
                    )
            finally:
                bridge.unregister(token)

        result = asyncio.run(run())

        self.assertTrue(result["ok"])
        self.assertEqual(
            result["params"]["message"][0]["data"]["file"],
            "base64://...(17 chars)",
        )

    def test_current_group_conversation_defaults_group_id_before_required_check(self) -> None:
        bridge = QQActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}

        async def run() -> None:
            token = bridge.register(asyncio.get_running_loop(), caller)
            conv_token = set_current_platform_conversation(ConversationKey("qq", "group", "100"))
            try:
                with patch("tools.tools.qq.functions.global_qq_action_bridge", bridge):
                    result = await asyncio.to_thread(
                        run_qq_function,
                        "send_group_msg",
                        {"message": "hi"},
                    )
            finally:
                reset_current_platform_conversation(conv_token)
                bridge.unregister(token)
            self.assertTrue(result["ok"])

        asyncio.run(run())
        self.assertEqual(calls, [("send_group_msg", {"message": "hi", "group_id": "100"})])

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
