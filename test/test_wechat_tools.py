"""微信 OC transport 与 wechattool 测试。"""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.event_bus import EventBus
from agent.platforms.context import (
    reset_current_platform_conversation,
    reset_current_platform_sender,
    set_current_platform_conversation,
    set_current_platform_sender,
)
from agent.platforms.messages import ConversationKey, OutboundMessage, OutboundSegment
from agent.wechat.action_bridge import WeChatActionBridge
from agent.wechat.adapter import WeChatOCAdapter
from agent.wechat.client import WeChatOCClient
from agent.wechat.config import WeChatConfig
from agent.wechat.oc_types import ITEM_IMAGE, ITEM_TEXT, build_media_send_body, build_text_send_body, parse_wechat_message
from tools.toolRegistry import ToolRegistry
from tools.tools.wechat.functions import run_wechat_function
from tools.tools.wechattool import WeChatTool


class TestWeChatConfig(unittest.TestCase):
    def test_env_defaults_and_overrides(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "WECHAT_ENABLE": "1",
                "WECHAT_BASE_URL": "https://example.com/",
                "WECHAT_STATE_FILE": "state.json",
                "CBAGENT_PLATFORM_ATTACHMENT_DIR_WECHAT": "attachments",
            },
            clear=False,
        ):
            cfg = WeChatConfig.from_env()
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.base_url, "https://example.com")
        self.assertEqual(cfg.state_file, Path("state.json"))
        self.assertEqual(cfg.attachment_dir, Path("attachments"))


class TestWeChatMessageParsing(unittest.TestCase):
    def test_private_text_message_parses(self) -> None:
        msg = {
            "from_user_id": "wxid_1",
            "message_id": 7,
            "context_token": "ctx",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "你好"}}],
        }
        inbound = parse_wechat_message(msg, WeChatConfig())
        self.assertIsNotNone(inbound)
        assert inbound is not None
        self.assertEqual(inbound.conversation, ConversationKey("wechat", "private", "wxid_1"))
        self.assertEqual(inbound.text, "你好")
        self.assertIn("sender_id=wxid_1", inbound.prompt_text())
        self.assertNotIn("sender_id=wxid_1", inbound.persistent_text())

    def test_group_message_is_ignored(self) -> None:
        base = {
            "from_user_id": "wxid_user",
            "group_id": "room_1",
            "item_list": [{"type": ITEM_TEXT, "text_item": {"text": "/agent 你好"}}],
        }
        self.assertIsNone(parse_wechat_message(base, WeChatConfig()))

    def test_image_item_becomes_attachment(self) -> None:
        msg = {
            "from_user_id": "wxid_1",
            "item_list": [
                {"type": ITEM_TEXT, "text_item": {"text": "看图"}},
                {"type": 2, "image_item": {"media": {"encrypt_query_param": "abc"}}},
            ],
        }
        inbound = parse_wechat_message(msg, WeChatConfig())
        self.assertIsNotNone(inbound)
        assert inbound is not None
        self.assertEqual(inbound.attachments[0].modality, "image")
        self.assertIn("微信图片", inbound.attachments[0].description)

    def test_text_send_body_shape(self) -> None:
        body = build_text_send_body(to_user_id="wxid_1", text="hi", context_token="ctx")
        self.assertEqual(body["msg"]["to_user_id"], "wxid_1")
        self.assertEqual(body["msg"]["context_token"], "ctx")
        self.assertEqual(body["msg"]["item_list"][0]["text_item"]["text"], "hi")

    def test_empty_text_body_omits_optional_fields(self) -> None:
        body = build_text_send_body(to_user_id="wxid_1", text="", context_token="")
        self.assertEqual(body["msg"]["to_user_id"], "wxid_1")
        self.assertNotIn("context_token", body["msg"])
        self.assertNotIn("item_list", body["msg"])

    def test_media_body_uses_single_item_without_caption(self) -> None:
        image_item = {"type": ITEM_IMAGE, "image_item": {"media": {"encrypt_query_param": "p"}}}
        body = build_media_send_body(
            to_user_id="wxid_1",
            item=image_item,
            context_token="ctx",
            caption="说明会由 adapter 单独发送",
        )
        self.assertEqual(body["msg"]["context_token"], "ctx")
        self.assertEqual(body["msg"]["item_list"], [image_item])


class TestWeChatClient(unittest.TestCase):
    def test_start_login_posts_local_token_list(self) -> None:
        client = WeChatOCClient(WeChatConfig(token="local-token", bot_type="3"))
        calls = []

        def fake_request(method, endpoint, **kwargs):  # noqa: ANN001
            calls.append((method, endpoint, kwargs))
            return {"qrcode": "qr", "qrcode_img_content": "url"}

        client.request_json = fake_request  # type: ignore[method-assign]
        result = client.start_login()

        self.assertEqual(result["qrcode"], "qr")
        self.assertEqual(calls[0][0], "POST")
        self.assertEqual(calls[0][1], "ilink/bot/get_bot_qrcode")
        self.assertEqual(calls[0][2]["params"], {"bot_type": "3"})
        self.assertEqual(calls[0][2]["payload"], {"local_token_list": ["local-token"]})

    def test_poll_login_sends_verify_code_only_when_present(self) -> None:
        client = WeChatOCClient(WeChatConfig())
        calls = []

        def fake_request(method, endpoint, **kwargs):  # noqa: ANN001
            calls.append(kwargs["params"])
            return {"status": "wait"}

        client.request_json = fake_request  # type: ignore[method-assign]
        client.poll_login("qr")
        client.poll_login("qr", "1234")

        self.assertEqual(calls[0], {"qrcode": "qr"})
        self.assertEqual(calls[1], {"qrcode": "qr", "verify_code": "1234"})

    def test_request_json_drops_none_fields(self) -> None:
        client = WeChatOCClient(WeChatConfig(base_url="https://example.com"))
        captured = []

        class Response:
            status_code = 200
            text = "{}"

            @staticmethod
            def json() -> dict:
                return {}

        def fake_request(*args, **kwargs):  # noqa: ANN001
            captured.append(kwargs["json"])
            return Response()

        with patch("agent.wechat.client.requests.request", fake_request):
            client.request_json(
                "POST",
                "ilink/bot/getconfig",
                payload={"keep": 1, "drop": None, "nested": {"drop": None, "keep": 2}},
            )

        self.assertEqual(captured[0], {"keep": 1, "nested": {"keep": 2}})


class TestWeChatTool(unittest.TestCase):
    def test_tool_schema_accepts_funname_and_args(self) -> None:
        tool = WeChatTool()
        params = {item.name: item for item in tool.get_parameters()}
        self.assertIn("funname", params)
        self.assertIn("args", params)
        self.assertTrue(tool.validate_parameters({"funname": "get_status", "args": {}}))
        self.assertTrue(tool.validate_parameters({"funname": "send_text", "args": "{\"text\":\"hi\"}"}))
        self.assertFalse(tool.validate_parameters({"funname": "", "args": {}}))
        self.assertFalse(tool.validate_parameters({"funname": "get_status", "args": []}))

    def test_run_auto_parses_json_string_args(self) -> None:
        tool = WeChatTool()

        def fake_run(funname, args):  # noqa: ANN001
            return {"ok": True, "funname": funname, "action": "send_text", "params": args, "duration_ms": 0}

        with patch("tools.tools.wechattool.run_wechat_function", fake_run):
            payload = json.loads(tool.run({
                "funname": "send_text",
                "args": "{\"text\":\"hi\"}",
            }))

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["params"], {"text": "hi"})
        self.assertTrue(payload["metadata"]["args_auto_parsed"])

    def test_unconnected_bridge_returns_clear_error(self) -> None:
        payload = json.loads(WeChatTool().run({"funname": "get_status", "args": {}}))
        self.assertFalse(payload["ok"])
        self.assertIn("WeChat OC transport is not running", payload["error"])

    def test_action_payload_is_sent_through_bridge_with_current_conversation(self) -> None:
        bridge = WeChatActionBridge()
        calls = []

        async def caller(action, params):  # noqa: ANN001
            calls.append((action, params))
            return {"ok": True, "data": {"sent": True}}

        async def run() -> None:
            token = bridge.register(asyncio.get_running_loop(), caller)
            conv_token = set_current_platform_conversation(ConversationKey("wechat", "private", "wxid_1"))
            try:
                with patch("tools.tools.wechat.functions.global_wechat_action_bridge", bridge):
                    result = await asyncio.to_thread(
                        run_wechat_function,
                        "send_text",
                        {"message": "hi"},
                    )
            finally:
                reset_current_platform_conversation(conv_token)
                bridge.unregister(token)
            self.assertTrue(result["ok"])

        asyncio.run(run())
        self.assertEqual(calls[0][0], "__cbagent_wechat_send_text__")
        self.assertEqual(calls[0][1]["user_id"], "wxid_1")
        self.assertEqual(calls[0][1]["text"], "hi")

    def test_registry_can_register_wechattool_without_qqtool(self) -> None:
        registry = ToolRegistry()
        registry.register_tool(WeChatTool())
        self.assertIn("wechattool", registry.list_tools())
        self.assertNotIn("qqtool", registry.list_tools())


class TestWeChatAdapter(unittest.TestCase):
    def test_state_roundtrip(self) -> None:
        class DummySession:
            question_registry = None

        with tempfile.TemporaryDirectory() as td:
            state_file = Path(td) / "wechat-state.json"
            cfg = WeChatConfig(
                token="env-token",
                account_id="bot-1",
                state_file=state_file,
                attachment_dir=Path(td) / "attachments",
            )
            adapter = WeChatOCAdapter(session=DummySession(), event_bus=EventBus(), config=cfg)
            adapter._sync_buf = "sync"
            adapter._context_tokens = {"wechat:private:wxid_1": "ctx"}
            adapter._save_state()

            loaded = WeChatOCAdapter(session=DummySession(), event_bus=EventBus(), config=WeChatConfig(state_file=state_file))
            loaded._load_state()
            self.assertEqual(loaded._token, "env-token")
            self.assertEqual(loaded._account_id, "bot-1")
            self.assertEqual(loaded._sync_buf, "sync")
            self.assertEqual(loaded._context_tokens["wechat:private:wxid_1"], "ctx")

    def test_send_outbound_text_uses_client(self) -> None:
        class DummySession:
            question_registry = None

        sent = []
        adapter = WeChatOCAdapter(
            session=DummySession(),
            event_bus=EventBus(),
            config=WeChatConfig(token="t"),
        )
        adapter._context_tokens = {"wechat:private:wxid_1": "ctx"}
        adapter.client.send_message = lambda body: sent.append(body) or {"ret": 0}  # type: ignore[method-assign]

        async def run() -> None:
            await adapter.send_outbound(OutboundMessage(
                conversation=ConversationKey("wechat", "private", "wxid_1"),
                segments=[OutboundSegment.text_segment("hello")],
            ))

        asyncio.run(run())
        self.assertEqual(sent[0]["msg"]["to_user_id"], "wxid_1")
        self.assertEqual(sent[0]["msg"]["context_token"], "ctx")
        self.assertEqual(sent[0]["msg"]["item_list"][0]["text_item"]["text"], "hello")

    def test_send_resource_with_caption_sends_text_then_single_media_item(self) -> None:
        class DummySession:
            question_registry = None

        sent = []
        adapter = WeChatOCAdapter(
            session=DummySession(),
            event_bus=EventBus(),
            config=WeChatConfig(token="t"),
        )
        adapter._context_tokens = {"wechat:private:wxid_1": "ctx"}
        adapter.client.prepare_media_item = lambda **kwargs: {  # type: ignore[method-assign]
            "type": ITEM_IMAGE,
            "image_item": {"media": {"encrypt_query_param": "p"}},
        }
        adapter.client.send_message = lambda body: sent.append(body) or {"ret": 0}  # type: ignore[method-assign]

        async def run() -> None:
            await adapter.send_outbound(OutboundMessage(
                conversation=ConversationKey("wechat", "private", "wxid_1"),
                segments=[OutboundSegment.file_segment(
                    kind="image",
                    path="demo.png",
                    text="这是一张图",
                )],
            ))

        asyncio.run(run())
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["msg"]["item_list"][0]["type"], ITEM_TEXT)
        self.assertEqual(sent[0]["msg"]["item_list"][0]["text_item"]["text"], "这是一张图")
        self.assertEqual(sent[1]["msg"]["item_list"][0]["type"], ITEM_IMAGE)
        self.assertEqual(len(sent[1]["msg"]["item_list"]), 1)

    def test_login_redirect_then_confirmed_updates_base_url_and_state(self) -> None:
        class DummySession:
            question_registry = None

        with tempfile.TemporaryDirectory() as td:
            adapter = WeChatOCAdapter(
                session=DummySession(),
                event_bus=EventBus(),
                config=WeChatConfig(
                    state_file=Path(td) / "wechat-state.json",
                    qr_poll_interval_seconds=0,
                ),
            )
            refreshes = []
            statuses = iter([
                {"status": "scaned_but_redirect", "redirect_host": "redirect.example.com"},
                {"status": "confirmed", "bot_token": "token-1", "ilink_bot_id": "bot-1"},
            ])

            async def fake_refresh() -> str:
                refreshes.append(True)
                return "qr"

            adapter._refresh_login_qrcode = fake_refresh  # type: ignore[method-assign]
            adapter.client.poll_login = lambda qrcode, verify_code="": next(statuses)  # type: ignore[method-assign]

            asyncio.run(adapter._ensure_login())

            self.assertEqual(len(refreshes), 1)
            self.assertEqual(adapter._token, "token-1")
            self.assertEqual(adapter._account_id, "bot-1")
            self.assertEqual(adapter.client.base_url, "https://redirect.example.com")


class TestWeChatPermission(unittest.TestCase):
    def test_wechat_platform_context_does_not_apply_root_gate(self) -> None:
        from agent.executor import ToolExecutor

        def tc(name: str, args: dict) -> dict:
            return {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }

        calls = []

        def runner(name, args):  # noqa: ANN001
            calls.append((name, args))
            return "{}"

        ex = ToolExecutor(runner)
        conv_token = set_current_platform_conversation(ConversationKey("wechat", "private", "wxid_current"))
        sender_token = set_current_platform_sender("wxid_other")
        try:
            result = ex.execute([
                tc("wechattool", {"funname": "send_text", "args": {"user_id": "wxid_other", "text": "hi"}}),
                tc("wechattool", {"funname": "get_login_info", "args": {}}),
            ])
        finally:
            reset_current_platform_sender(sender_token)
            reset_current_platform_conversation(conv_token)

        self.assertEqual([name for name, _ in calls], ["wechattool", "wechattool"])
        self.assertTrue(all(not item.is_error for item in result))


if __name__ == "__main__":
    unittest.main()
