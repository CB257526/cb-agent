"""QQ/NapCat OneBot 适配器测试。"""

from __future__ import annotations

import asyncio
import base64
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from agent.event_bus import EventBus
from agent.platforms.context import get_current_platform_conversation
from agent.platforms.messages import ConversationKey, InboundAttachment, InboundMessage, OutboundMessage, OutboundSegment
from agent.qq.adapter import _action_ok
from agent.qq.config import QQConfig
from agent.qq.onebot import outbound_segment_to_onebot, parse_onebot_event, parse_onebot_message_event
from agent.question_registry import QuestionRegistry


class TestOneBotParsing(unittest.TestCase):
    def test_private_text_message_parses(self) -> None:
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123,
            "message_id": 9,
            "sender": {"nickname": "小明"},
            "message": [{"type": "text", "data": {"text": "你好"}}],
        }
        msg = parse_onebot_message_event(event, QQConfig())
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.conversation, ConversationKey("qq", "private", "123"))
        self.assertEqual(msg.text, "你好")

    def test_group_mention_required_by_default(self) -> None:
        cfg = QQConfig(group_mode="mention")
        base = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 999,
            "group_id": 456,
            "user_id": 123,
            "sender": {"card": "群友"},
        }
        no_mention = dict(base, message=[{"type": "text", "data": {"text": "你好"}}])
        mentioned = dict(base, message=[
            {"type": "at", "data": {"qq": "999"}},
            {"type": "text", "data": {"text": " 帮我查一下"}},
        ])
        self.assertIsNone(parse_onebot_message_event(no_mention, cfg))
        msg = parse_onebot_message_event(mentioned, cfg)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.conversation, ConversationKey("qq", "group", "456"))
        self.assertEqual(msg.text, "帮我查一下")

    def test_group_prefix_and_pending_question_bypass_wakeup(self) -> None:
        cfg = QQConfig(group_mode="prefix", wake_prefix="/agent")
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 456,
            "user_id": 123,
            "message": [{"type": "text", "data": {"text": "/agent 你好"}}],
        }
        msg = parse_onebot_message_event(event, cfg)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.text, "你好")

        reply = dict(event, message=[{"type": "text", "data": {"text": "1"}}])
        self.assertIsNone(parse_onebot_message_event(reply, cfg, require_wakeup=True))
        bypass = parse_onebot_message_event(reply, cfg, require_wakeup=False)
        self.assertIsNotNone(bypass)
        assert bypass is not None
        self.assertEqual(bypass.text, "1")

    def test_whitelist_filters(self) -> None:
        cfg = QQConfig(allowed_groups={"1"}, allowed_users={"2"}, group_mode="all")
        event = {
            "post_type": "message",
            "message_type": "group",
            "group_id": 9,
            "user_id": 2,
            "message": [{"type": "text", "data": {"text": "hi"}}],
        }
        self.assertIsNone(parse_onebot_message_event(event, cfg))

    def test_image_segment_becomes_attachment_description(self) -> None:
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123,
            "message": [
                {"type": "text", "data": {"text": "看图"}},
                {"type": "image", "data": {"file": "a.jpg", "url": "http://x/a.jpg"}},
            ],
        }
        msg = parse_onebot_message_event(event, QQConfig())
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.attachments[0].modality, "image")
        self.assertIn("http://x/a.jpg", msg.prompt_text())

    def test_cq_string_message_parses_mention_and_image(self) -> None:
        cfg = QQConfig(group_mode="mention")
        event = {
            "post_type": "message",
            "message_type": "group",
            "self_id": 999,
            "group_id": 456,
            "user_id": 123,
            "message": "[CQ:at,qq=999] 看图[CQ:image,file=a.jpg,url=http://x/a.jpg]",
        }
        msg = parse_onebot_message_event(event, cfg)
        self.assertIsNotNone(msg)
        assert msg is not None
        self.assertEqual(msg.text, "看图")
        self.assertEqual(msg.attachments[0].modality, "image")
        self.assertEqual(msg.attachments[0].file_name, "a.jpg")
        self.assertIn("http://x/a.jpg", msg.prompt_text())

    def test_outbound_segments_convert_to_onebot(self) -> None:
        text = outbound_segment_to_onebot(OutboundSegment.text_segment("hi"))
        image = outbound_segment_to_onebot(OutboundSegment.file_segment(kind="sticker", path="C:/tmp/a.png"))
        file_seg = outbound_segment_to_onebot(OutboundSegment.file_segment(kind="file", path="C:/tmp/a.zip"))
        self.assertEqual(text[0]["type"], "text")
        self.assertEqual(image[0]["type"], "image")
        self.assertTrue(image[0]["data"]["file"].startswith("file:///"))
        self.assertEqual(file_seg[0]["type"], "file")

    def test_notice_group_upload_is_ignored(self) -> None:
        """群文件上传属于平台通知，不应触发 agent，避免机器人发文件后自激活。"""

        event = {
            "post_type": "notice",
            "notice_type": "group_upload",
            "group_id": 100,
            "user_id": 200,
            "file": {"id": "fid-1", "name": "report.zip", "size": 12},
        }
        self.assertIsNone(parse_onebot_event(event, QQConfig()))

    def test_notice_input_status_is_ignored(self) -> None:
        """NapCat 的“对方正在输入”只是客户端状态，不能触发 agent 对话。"""

        event = {
            "post_type": "notice",
            "notice_type": "input_status",
            "sub_type": "input_status",
            "user_id": 2978048948,
            "status": "inputting",
        }
        self.assertIsNone(parse_onebot_event(event, QQConfig()))

    def test_unknown_notice_is_ignored_by_default(self) -> None:
        """未知 notice 默认静默，避免把平台噪声误当成用户请求。"""

        event = {
            "post_type": "notice",
            "notice_type": "client_status",
            "user_id": 2978048948,
            "online": True,
        }
        self.assertIsNone(parse_onebot_event(event, QQConfig()))

    def test_known_non_message_events_are_ignored(self) -> None:
        """戳一戳、群成员变化等 notice 也不带明确对话意图，默认不触发 agent。"""

        events = [
            {
                "post_type": "notice",
                "notice_type": "notify",
                "sub_type": "poke",
                "group_id": 100,
                "user_id": 200,
                "target_id": 999,
            },
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "group_id": 100,
                "user_id": 200,
                "operator_id": 300,
            },
            {
                "post_type": "notice",
                "notice_type": "friend_add",
                "user_id": 200,
            },
        ]
        for event in events:
            with self.subTest(event=event):
                self.assertIsNone(parse_onebot_event(event, QQConfig()))

    def test_request_event_is_ignored_by_default(self) -> None:
        """好友/加群申请是管理事件，首版不喂给模型，避免无上下文误回复。"""

        event = {
            "post_type": "request",
            "request_type": "friend",
            "user_id": 200,
            "comment": "我是测试用户",
            "flag": "flag-1",
        }
        self.assertIsNone(parse_onebot_event(event, QQConfig()))


class TestQQAdapterSend(unittest.TestCase):
    def test_action_ok_treats_failed_status_as_failure(self) -> None:
        self.assertTrue(_action_ok({"status": "ok", "retcode": 0}))
        self.assertTrue(_action_ok({"status": "async"}))
        self.assertFalse(_action_ok({"status": "failed", "retcode": 0}))
        self.assertFalse(_action_ok({"retcode": 1404}))

    def test_send_outbound_uses_group_and_private_actions(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self) -> None:
                self.calls = []

            async def call_action(self, action, params):  # type: ignore[override]
                self.calls.append((action, params))
                return {"status": "ok", "retcode": 0}

        adapter = DummyAdapter()

        async def run() -> None:
            with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as fh:
                fh.write(b"hello")
                file_path = fh.name
            try:
                await adapter.send_outbound(OutboundMessage.text(
                    ConversationKey("qq", "group", "100"),
                    "hello",
                ))
                await adapter.send_outbound(OutboundMessage(
                    conversation=ConversationKey("qq", "private", "200"),
                    segments=[OutboundSegment.file_segment(kind="file", path=file_path)],
                ))
            finally:
                Path(file_path).unlink(missing_ok=True)

        asyncio.run(run())
        self.assertEqual(adapter.calls[0][0], "send_group_msg")
        self.assertEqual(adapter.calls[0][1]["group_id"], 100)
        self.assertEqual(adapter.calls[1][0], "upload_private_file")
        self.assertEqual(adapter.calls[1][1]["user_id"], 200)

    def test_failed_file_upload_degrades_to_text_message(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self) -> None:
                self.calls = []

            async def call_action(self, action, params):  # type: ignore[override]
                self.calls.append((action, params))
                if action == "upload_group_file":
                    return {"status": "failed", "retcode": 1404}
                return {"status": "ok", "retcode": 0}

        adapter = DummyAdapter()

        async def run() -> None:
            with tempfile.NamedTemporaryFile("wb", suffix=".txt", delete=False) as fh:
                fh.write(b"hello")
                file_path = fh.name
            try:
                await adapter.send_outbound(OutboundMessage(
                    conversation=ConversationKey("qq", "group", "100"),
                    segments=[OutboundSegment.file_segment(kind="file", path=file_path)],
                ))
            finally:
                Path(file_path).unlink(missing_ok=True)

        asyncio.run(run())
        self.assertEqual(adapter.calls[0][0], "upload_group_file")
        self.assertEqual(adapter.calls[1][0], "send_group_msg")
        fallback_text = adapter.calls[1][1]["message"][0]["data"]["text"]
        self.assertIn("文件发送失败", fallback_text)
        self.assertIn("QQ_FILE_DELIVERY_MODE", fallback_text)

    def test_mapped_path_delivery_sends_container_visible_path(self) -> None:
        """Docker 场景下先复制到共享目录，再把路径改写成 NapCat 容器内路径。"""
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self, config: QQConfig) -> None:
                self.calls = []
                self.config = config

            async def call_action(self, action, params):  # type: ignore[override]
                self.calls.append((action, params))
                return {"status": "ok", "retcode": 0}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.txt"
            host_shared = root / "shared"
            source.write_text("docker file", encoding="utf-8")
            adapter = DummyAdapter(QQConfig(
                file_delivery_mode="mapped_path",
                file_host_prefix=str(host_shared),
                file_napcat_prefix="/app/outbound",
            ))

            async def run() -> None:
                await adapter.send_outbound(OutboundMessage(
                    conversation=ConversationKey("qq", "group", "100"),
                    segments=[OutboundSegment.file_segment(kind="file", path=str(source))],
                ))

            asyncio.run(run())

            self.assertEqual(adapter.calls[0][0], "upload_group_file")
            sent_file = adapter.calls[0][1]["file"]
            self.assertTrue(str(sent_file).startswith("/app/outbound/"))
            self.assertNotIn(str(root), str(sent_file))
            copied = list(host_shared.glob("source-*.txt"))
            self.assertEqual(len(copied), 1)
            self.assertEqual(copied[0].read_text(encoding="utf-8"), "docker file")

    def test_media_segment_delivery_preserves_http_and_container_path(self) -> None:
        """图片/表情这类消息段也要使用交付层结果，不能强制变成宿主机 file://。"""
        from agent.qq.onebot import outbound_segment_to_onebot

        http_seg = outbound_segment_to_onebot(OutboundSegment(kind="image", path="http://host/a.png"))
        container_seg = outbound_segment_to_onebot(OutboundSegment(kind="image", path="/app/outbound/a.png"))

        self.assertEqual(http_seg[0]["data"]["file"], "http://host/a.png")
        self.assertEqual(container_seg[0]["data"]["file"], "/app/outbound/a.png")


class TestQQFileDeliveryManager(unittest.TestCase):
    def test_path_delivery_keeps_original_behavior(self) -> None:
        from agent.qq.file_delivery import QQFileDeliveryManager

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "a.txt"
            source.write_text("hello", encoding="utf-8")
            manager = QQFileDeliveryManager(QQConfig(file_delivery_mode="path"))
            plan = manager.build_plan(str(source))

        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.candidates[0].method, "path")
        self.assertEqual(Path(plan.candidates[0].ref), source.resolve())

    def test_mapped_path_delivery_copies_file_and_rewrites_path(self) -> None:
        from agent.qq.file_delivery import QQFileDeliveryManager

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "report.txt"
            host_shared = root / "shared"
            source.write_text("mapped", encoding="utf-8")
            manager = QQFileDeliveryManager(QQConfig(
                file_delivery_mode="mapped_path",
                file_host_prefix=str(host_shared),
                file_napcat_prefix="/app/outbound",
            ))
            plan = manager.build_plan(str(source))

            self.assertEqual(plan.errors, [])
            self.assertEqual(plan.candidates[0].method, "mapped_path")
            self.assertTrue(plan.candidates[0].ref.startswith("/app/outbound/"))
            copied = list(host_shared.glob("report-*.txt"))
            self.assertEqual(len(copied), 1)
            self.assertEqual(copied[0].read_text(encoding="utf-8"), "mapped")

    def test_http_delivery_serves_temporary_url(self) -> None:
        from agent.qq.file_delivery import QQFileDeliveryManager

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "asset.txt"
            source.write_bytes(b"http body")
            manager = QQFileDeliveryManager(QQConfig(
                file_delivery_mode="http",
                file_http_host="127.0.0.1",
                file_http_port=0,
                file_http_ttl_seconds=60,
            ))
            try:
                plan = manager.build_plan(str(source))
                self.assertEqual(plan.errors, [])
                self.assertEqual(plan.candidates[0].method, "http")
                with urllib.request.urlopen(plan.candidates[0].ref, timeout=5) as response:
                    self.assertEqual(response.read(), b"http body")
            finally:
                manager.close()

    def test_base64_delivery_inlines_small_file(self) -> None:
        from agent.qq.file_delivery import QQFileDeliveryManager

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "small.txt"
            source.write_bytes(b"small")
            manager = QQFileDeliveryManager(QQConfig(
                file_delivery_mode="base64",
                file_base64_max_mb=1,
            ))
            plan = manager.build_plan(str(source))

        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.candidates[0].method, "base64")
        payload = plan.candidates[0].ref.removeprefix("base64://")
        self.assertEqual(base64.b64decode(payload), b"small")

    def test_auto_delivery_builds_fallback_candidates(self) -> None:
        from agent.qq.file_delivery import QQFileDeliveryManager

        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "small.txt"
            source.write_bytes(b"small")
            manager = QQFileDeliveryManager(QQConfig(
                file_delivery_mode="auto",
                file_http_host="0.0.0.0",
                file_base64_max_mb=1,
            ))
            plan = manager.build_plan(str(source))

        self.assertTrue(any("QQ_FILE_HOST_PREFIX" in item for item in plan.errors))
        self.assertTrue(any("QQ_FILE_HTTP_PUBLIC_BASE_URL" in item for item in plan.errors))
        self.assertEqual([item.method for item in plan.candidates], ["base64", "path"])

    def test_materialize_inbound_attachment_downloads_url_to_local_file(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self, attachment_dir: Path) -> None:
                self._attachment_dir = attachment_dir

        class DummyResponse:
            headers = {"content-length": "7", "content-type": "image/png"}

            def raise_for_status(self) -> None:
                return None

            def iter_content(self, chunk_size: int):  # noqa: ARG002 - 模拟 requests 的迭代接口
                yield b"png"
                yield b"data"

        with tempfile.TemporaryDirectory() as td:
            adapter = DummyAdapter(Path(td))
            inbound = InboundMessage(
                conversation=ConversationKey("qq", "private", "123"),
                sender_id="123",
                sender_name="小明",
                text="看图",
                attachments=[
                    InboundAttachment(
                        modality="image",
                        url="http://example.test/a.png",
                        file_name="a.png",
                        description="QQ 图片 a.png",
                    )
                ],
            )

            with patch("agent.qq.adapter.requests.get", return_value=DummyResponse()) as get:
                asyncio.run(adapter._materialize_inbound_attachments(inbound))

            get.assert_called_once_with("http://example.test/a.png", timeout=20, stream=True)
            self.assertIsNotNone(inbound.attachments[0].path)
            saved = Path(str(inbound.attachments[0].path))
            self.assertTrue(saved.exists())
            self.assertEqual(saved.read_bytes(), b"pngdata")
            self.assertEqual(inbound.prompt_attachments()[0]["path"], str(saved.resolve()))

    def test_file_id_resolves_with_napcat_file_url_action(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self) -> None:
                self.calls = []

            async def call_action(self, action, params):  # type: ignore[override]
                self.calls.append((action, params))
                return {"status": "ok", "data": {"url": "https://files.example/report.zip", "file_name": "report.zip"}}

        adapter = DummyAdapter()
        inbound = InboundMessage(
            conversation=ConversationKey("qq", "group", "100"),
            sender_id="200",
            sender_name="群友",
            text="",
            attachments=[InboundAttachment(modality="file", file_id="fid-1", file_name="old.bin")],
        )

        asyncio.run(adapter._resolve_inbound_file_urls(inbound))

        self.assertEqual(adapter.calls[0][0], "get_group_file_url")
        self.assertEqual(adapter.calls[0][1]["group_id"], 100)
        self.assertEqual(inbound.attachments[0].url, "https://files.example/report.zip")
        self.assertEqual(inbound.attachments[0].file_name, "report.zip")

    def test_reply_message_is_appended_to_prompt_text(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummyAdapter(QQNapCatAdapter):
            def __init__(self) -> None:
                self.config = QQConfig(group_mode="all")

            async def call_action(self, action, params):  # type: ignore[override]
                self.action = (action, params)
                return {
                    "status": "ok",
                    "data": {
                        "message_id": 9,
                        "user_id": 333,
                        "sender": {"nickname": "引用者"},
                        "message": [{"type": "text", "data": {"text": "被引用内容"}}],
                    },
                }

        adapter = DummyAdapter()
        inbound = InboundMessage(
            conversation=ConversationKey("qq", "group", "100"),
            sender_id="200",
            sender_name="群友",
            text="请总结",
            reply_to_message_id="9",
        )

        asyncio.run(adapter._append_reply_message_summary(inbound))

        self.assertEqual(adapter.action[0], "get_msg")
        self.assertIn("被引用内容", inbound.text)
        self.assertIn("请总结", inbound.text)

    def test_conversation_sessions_are_isolated_and_context_is_bound(self) -> None:
        from agent.qq.adapter import QQNapCatAdapter

        class DummySession:
            def __init__(self, name: str) -> None:
                self.name = name
                self.question_registry = QuestionRegistry()
                self.calls = []

            async def chat_async(  # noqa: ANN001
                self,
                text,
                cancel_token=None,
                attachments=None,
                persistent_user_text=None,
            ):
                self.calls.append((
                    text,
                    attachments,
                    get_current_platform_conversation(),
                    persistent_user_text,
                ))
                await asyncio.sleep(0.01)
                return self.name

        main = DummySession("main")
        sessions: dict[str, DummySession] = {}

        def factory(conversation: ConversationKey) -> DummySession:
            session = DummySession(conversation.stable_id)
            sessions[conversation.stable_id] = session
            return session

        adapter = QQNapCatAdapter(
            session=main,  # type: ignore[arg-type]
            session_factory=factory,  # type: ignore[arg-type]
            event_bus=EventBus(),
            config=QQConfig(),
        )
        conv_a = ConversationKey("qq", "group", "100")
        conv_b = ConversationKey("qq", "private", "200")

        async def run() -> None:
            await asyncio.gather(
                adapter._start_agent_run(InboundMessage(conv_a, "1", "A", "来自 A")),
                adapter._start_agent_run(InboundMessage(conv_b, "2", "B", "来自 B")),
            )

        try:
            asyncio.run(run())
        finally:
            adapter._renderer.close()

        self.assertIsNot(sessions[conv_a.stable_id], sessions[conv_b.stable_id])
        self.assertEqual(sessions[conv_a.stable_id].calls[0][2], conv_a)
        self.assertEqual(sessions[conv_b.stable_id].calls[0][2], conv_b)
        self.assertEqual(sessions[conv_a.stable_id].calls[0][3], "来自 A")
        self.assertEqual(sessions[conv_b.stable_id].calls[0][3], "来自 B")

    def test_private_conversation_refreshes_session_object_every_message(self) -> None:
        """私聊每条消息都创建新的 AgentSession 对象，由对象构造时从磁盘恢复上下文。"""
        from agent.qq.adapter import QQNapCatAdapter

        class DummySession:
            def __init__(self, name: str) -> None:
                self.name = name
                self.question_registry = QuestionRegistry()
                self.calls = []

            async def chat_async(  # noqa: ANN001
                self,
                text,
                cancel_token=None,
                attachments=None,
                persistent_user_text=None,
            ):
                self.calls.append((text, persistent_user_text))
                return self.name

        main = DummySession("main")
        created: list[DummySession] = []

        def factory(conversation: ConversationKey) -> DummySession:
            session = DummySession(f"{conversation.stable_id}-{len(created)}")
            created.append(session)
            return session

        adapter = QQNapCatAdapter(
            session=main,  # type: ignore[arg-type]
            session_factory=factory,  # type: ignore[arg-type]
            event_bus=EventBus(),
            config=QQConfig(),
        )
        conv = ConversationKey("qq", "private", "200")

        async def run() -> None:
            await adapter._start_agent_run(InboundMessage(conv, "200", "好友", "第一条"))
            await adapter._start_agent_run(InboundMessage(conv, "200", "好友", "第二条"))

        try:
            asyncio.run(run())
        finally:
            adapter._renderer.close()

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertEqual(len(created[0].calls), 1)
        self.assertEqual(len(created[1].calls), 1)
        self.assertIn("第一条", created[0].calls[0][0])
        self.assertEqual(created[0].calls[0][1], "第一条")
        self.assertIn("第二条", created[1].calls[0][0])
        self.assertEqual(created[1].calls[0][1], "第二条")

    def test_group_conversation_uses_ephemeral_session_every_message(self) -> None:
        """群聊消息量大，默认每条消息使用临时 AgentSession，不在适配器里缓存。"""
        from agent.qq.adapter import QQNapCatAdapter

        class DummySession:
            def __init__(self, name: str) -> None:
                self.name = name
                self.question_registry = QuestionRegistry()
                self.calls = []

            async def chat_async(  # noqa: ANN001
                self,
                text,
                cancel_token=None,
                attachments=None,
                persistent_user_text=None,
            ):
                self.calls.append((text, persistent_user_text))
                return self.name

        main = DummySession("main")
        created: list[DummySession] = []

        def factory(conversation: ConversationKey) -> DummySession:
            session = DummySession(f"{conversation.stable_id}-{len(created)}")
            created.append(session)
            return session

        adapter = QQNapCatAdapter(
            session=main,  # type: ignore[arg-type]
            session_factory=factory,  # type: ignore[arg-type]
            event_bus=EventBus(),
            config=QQConfig(),
        )
        conv = ConversationKey("qq", "group", "100")

        async def run() -> None:
            await adapter._start_agent_run(InboundMessage(conv, "1", "群友", "第一条"))
            await adapter._start_agent_run(InboundMessage(conv, "2", "群友", "第二条"))

        try:
            asyncio.run(run())
        finally:
            adapter._renderer.close()

        self.assertEqual(len(created), 2)
        self.assertIsNot(created[0], created[1])
        self.assertEqual(len(created[0].calls), 1)
        self.assertEqual(len(created[1].calls), 1)
        self.assertIn("第一条", created[0].calls[0][0])
        self.assertEqual(created[0].calls[0][1], "第一条")
        self.assertIn("第二条", created[1].calls[0][0])
        self.assertEqual(created[1].calls[0][1], "第二条")

    def test_same_conversation_messages_are_queued_in_order(self) -> None:
        """同一 QQ 会话内消息排队处理，不再因为上一条未完成而直接拒绝。"""
        from agent.qq.adapter import QQNapCatAdapter

        class SlowSession:
            def __init__(self) -> None:
                self.question_registry = QuestionRegistry()
                self.calls = []

            async def chat_async(  # noqa: ANN001
                self,
                text,
                cancel_token=None,
                attachments=None,
                persistent_user_text=None,
            ):
                self.calls.append(text)
                await asyncio.sleep(0.05)
                return "ok"

        class CollectingAdapter(QQNapCatAdapter):
            async def send_outbound(self, message):  # type: ignore[override]
                self.sent.append(message)

        session = SlowSession()
        adapter = CollectingAdapter(
            session=session,  # type: ignore[arg-type]
            event_bus=EventBus(),
            config=QQConfig(),
        )
        adapter.sent = []
        conv = ConversationKey("qq", "group", "100")

        async def run() -> None:
            first = asyncio.create_task(adapter._start_agent_run(InboundMessage(conv, "1", "A", "第一条")))
            await asyncio.sleep(0.01)
            await adapter._start_agent_run(InboundMessage(conv, "1", "A", "第二条"))
            await first

        try:
            asyncio.run(run())
        finally:
            adapter._renderer.close()

        self.assertEqual(len(session.calls), 2)
        self.assertIn("第一条", session.calls[0])
        self.assertIn("第二条", session.calls[1])
        self.assertEqual(adapter.sent, [])


if __name__ == "__main__":
    unittest.main()
