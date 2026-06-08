"""通讯平台事件渲染器测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.event_bus import EventBus
from agent.events import (
    AskUserQuestion,
    AskUserQuestionAnswered,
    BackgroundNotification,
    Done,
    Error,
    ReasoningDelta,
    RoundEnd,
    TodoListUpdated,
    ToolComplete,
    ToolStart,
)
from agent.platforms.context import reset_current_platform_conversation, set_current_platform_conversation
from agent.platforms.messages import ConversationKey, OutboundMessage
from agent.platforms.renderer import PlatformEventRenderer
from agent.question_registry import QuestionRegistry
from tools.tools.todo_tool import TodoTool
from tools.tools.send_message_asset_tool import SendMessageAssetTool


class TestPlatformEventRenderer(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus()
        self.sent: list[OutboundMessage] = []
        self.conversation = ConversationKey(platform="qq", kind="group", id="10001")
        self.renderer = PlatformEventRenderer(
            event_bus=self.bus,
            send=self.sent.append,
            verbosity="normal",
            confirm_question_answer=True,
            group_tool_messages=True,
        )
        self.renderer.begin_run(self.conversation)

    def tearDown(self) -> None:
        self.renderer.close()

    def test_done_error_background_and_todo_render_to_text(self) -> None:
        self.bus.emit(Done(final_answer="最终回答", rounds_used=1))
        self.bus.emit(Error(where="session", message="坏了"))
        self.bus.emit(BackgroundNotification(task_id="t1", status="done", exit_code=0, output_path="out.txt"))
        self.bus.emit(TodoListUpdated(items=[{"id": "1", "content": "写测试", "status": "in_progress"}]))

        texts = [msg.segments[0].text for msg in self.sent]
        self.assertIn("最终回答", texts[0])
        self.assertIn("坏了", texts[1])
        self.assertIn("后台任务 t1", texts[2])
        self.assertIn("写测试", texts[3])

    def test_tool_start_renders_by_default(self) -> None:
        self.bus.emit(ToolStart(
            call_id="c1",
            name="grep",
            arguments={"pattern": "TODO", "api_key": "secret-value"},
        ))

        text = self.sent[-1].segments[0].text
        self.assertIn("调用工具:grep", text)
        self.assertIn("TODO", text)
        self.assertNotIn("secret-value", text)
        self.assertIn("<已脱敏>", text)

    def test_bash_tool_start_renders_command(self) -> None:
        self.bus.emit(ToolStart(
            call_id="c1",
            name="bash",
            arguments={"command": "python -m pytest test/test_platform_events.py"},
        ))

        self.assertEqual(
            self.sent[-1].segments[0].text,
            "（执行命令:python -m pytest test/test_platform_events.py）",
        )

    def test_group_tool_messages_can_be_disabled_by_env(self) -> None:
        self.renderer.close()
        self.sent.clear()
        with patch.dict("os.environ", {"IM_GROUP_TOOL_MESSAGES": "0"}):
            self.renderer = PlatformEventRenderer(
                event_bus=self.bus,
                send=self.sent.append,
                verbosity="normal",
            )
        self.renderer.begin_run(self.conversation)

        self.bus.emit(ToolStart(call_id="c1", name="grep", arguments={"pattern": "TODO"}))

        self.assertEqual(self.sent, [])

    def test_private_tool_messages_ignore_group_switch(self) -> None:
        self.renderer.close()
        self.sent.clear()
        private = ConversationKey(platform="qq", kind="private", id="20002")
        with patch.dict("os.environ", {"IM_GROUP_TOOL_MESSAGES": "0"}):
            self.renderer = PlatformEventRenderer(
                event_bus=self.bus,
                send=self.sent.append,
                verbosity="normal",
            )
        self.renderer.begin_run(private)

        self.bus.emit(ToolStart(call_id="c1", name="grep", arguments={"pattern": "TODO"}))

        self.assertEqual(len(self.sent), 1)
        self.assertIn("调用工具:grep", self.sent[0].segments[0].text)

    def test_full_tool_progress_respects_group_switch(self) -> None:
        self.renderer.close()
        self.sent.clear()
        with patch.dict("os.environ", {"IM_GROUP_TOOL_MESSAGES": "0"}):
            self.renderer = PlatformEventRenderer(
                event_bus=self.bus,
                send=self.sent.append,
                verbosity="full",
            )
        self.renderer.begin_run(self.conversation)

        self.bus.emit(ToolComplete(
            call_id="c1",
            name="grep",
            result="{}",
            duration_seconds=0.2,
        ))

        self.assertEqual(self.sent, [])

    def test_tool_complete_renders_when_full(self) -> None:
        self.renderer.close()
        self.sent.clear()
        self.renderer = PlatformEventRenderer(
            event_bus=self.bus,
            send=self.sent.append,
            verbosity="full",
            group_tool_messages=True,
        )
        self.renderer.begin_run(self.conversation)

        self.bus.emit(ToolComplete(
            call_id="c1",
            name="grep",
            result="{}",
            duration_seconds=0.2,
        ))
        self.assertEqual(self.sent[0].segments[0].text, "工具完成：grep，耗时 0.20s")

    def test_reasoning_delta_is_hidden_by_default(self) -> None:
        self.bus.emit(ReasoningDelta(delta="先想一下", accumulated="先想一下", round_idx=1))
        self.bus.emit(RoundEnd(round_idx=1, has_tool_calls=False, final=True))

        self.assertEqual(self.sent, [])

    def test_reasoning_delta_renders_when_enabled_on_round_end(self) -> None:
        self.renderer.close()
        self.sent.clear()
        self.renderer = PlatformEventRenderer(
            event_bus=self.bus,
            send=self.sent.append,
            verbosity="normal",
            show_reasoning=True,
            group_tool_messages=True,
        )
        self.renderer.begin_run(self.conversation)

        self.bus.emit(ReasoningDelta(delta="先分析问题。", accumulated="先分析问题。", round_idx=1))
        self.bus.emit(ReasoningDelta(delta="再调用工具。", accumulated="先分析问题。再调用工具。", round_idx=1))
        self.bus.emit(RoundEnd(round_idx=1, has_tool_calls=False, final=True))

        self.assertEqual(len(self.sent), 1)
        text = self.sent[0].segments[0].text
        self.assertIn("【思考｜第 1 轮】", text)
        self.assertIn("先分析问题。再调用工具。", text)

    def test_reasoning_delta_flushes_before_tool_start(self) -> None:
        self.renderer.close()
        self.sent.clear()
        self.renderer = PlatformEventRenderer(
            event_bus=self.bus,
            send=self.sent.append,
            verbosity="normal",
            show_reasoning=True,
            group_tool_messages=True,
        )
        self.renderer.begin_run(self.conversation)

        self.bus.emit(ReasoningDelta(delta="需要先搜索。", accumulated="需要先搜索。", round_idx=2))
        self.bus.emit(ToolStart(call_id="c1", name="grep", arguments={"pattern": "TODO"}, round_idx=2))

        self.assertEqual(len(self.sent), 2)
        self.assertIn("需要先搜索。", self.sent[0].segments[0].text)
        self.assertIn("调用工具:grep", self.sent[1].segments[0].text)

    def test_reasoning_delta_respects_max_chars(self) -> None:
        self.renderer.close()
        self.sent.clear()
        with patch.dict("os.environ", {
            "IM_REASONING_MAX_CHARS": "10",
            "IM_REASONING_CHUNK_CHARS": "200",
        }):
            self.renderer = PlatformEventRenderer(
                event_bus=self.bus,
                send=self.sent.append,
                verbosity="normal",
                show_reasoning=True,
            )
        self.renderer.begin_run(self.conversation)

        self.bus.emit(ReasoningDelta(delta="1234567890abcdef", accumulated="1234567890abcdef", round_idx=1))
        self.bus.emit(RoundEnd(round_idx=1, has_tool_calls=False, final=True))

        self.assertEqual(len(self.sent), 1)
        text = self.sent[0].segments[0].text
        self.assertIn("1234567890", text)
        self.assertIn("后续已省略", text)
        self.assertNotIn("abcdef", text)

    def test_ask_user_question_supports_number_reply(self) -> None:
        registry = QuestionRegistry()
        registry.register("q1")

        self.bus.emit(AskUserQuestion(
            question_id="q1",
            question="选哪个？",
            options=[
                {"label": "A", "description": "第一个"},
                {"label": "B", "description": "第二个"},
            ],
        ))

        self.assertIn("回复编号", self.sent[-1].segments[0].text)
        consumed = self.renderer.try_answer_pending(
            conversation=self.conversation,
            text="2",
            registry=registry,
        )
        self.assertTrue(consumed)
        slot = registry.wait_for_answer("q1", timeout=0.01)
        self.assertEqual(slot.selected_labels, ["B"])
        registry.discard("q1")

    def test_group_question_reply_requires_requesting_user(self) -> None:
        """群聊里只有发起请求的用户可以回复编号确认，避免他人代点权限。"""

        registry = QuestionRegistry()
        registry.register("q_guard")
        self.renderer.begin_run(self.conversation, sender_id="user_a")

        self.bus.emit(AskUserQuestion(
            question_id="q_guard",
            question="是否执行命令？",
            options=[
                {"label": "允许这一次", "description": "只执行本次"},
                {"label": "拒绝", "description": "取消本次"},
            ],
        ))

        consumed = self.renderer.try_answer_pending(
            conversation=self.conversation,
            text="1",
            registry=registry,
            sender_id="user_b",
        )
        self.assertTrue(consumed)
        self.assertIn("只有发起这次请求的用户", self.sent[-1].segments[0].text)

        consumed = self.renderer.try_answer_pending(
            conversation=self.conversation,
            text="1",
            registry=registry,
            sender_id="user_a",
        )
        self.assertTrue(consumed)
        slot = registry.wait_for_answer("q_guard", timeout=0.01)
        self.assertEqual(slot.selected_labels, ["允许这一次"])
        registry.discard("q_guard")

    def test_number_reply_keeps_pending_until_answered_event_clears_it(self) -> None:
        registry = QuestionRegistry()
        registry.register("q_pending")

        self.bus.emit(AskUserQuestion(
            question_id="q_pending",
            question="选哪个？",
            options=[
                {"label": "A", "description": "第一个"},
                {"label": "B", "description": "第二个"},
            ],
        ))

        self.assertTrue(self.renderer.has_pending_question(self.conversation))
        consumed = self.renderer.try_answer_pending(
            conversation=self.conversation,
            text="1",
            registry=registry,
        )
        self.assertTrue(consumed)
        self.assertTrue(self.renderer.has_pending_question(self.conversation))

        slot = registry.wait_for_answer("q_pending", timeout=0.01)
        self.bus.emit(AskUserQuestionAnswered(
            question_id="q_pending",
            selected_labels=list(slot.selected_labels),
        ))
        self.assertFalse(self.renderer.has_pending_question(self.conversation))
        registry.discard("q_pending")

    def test_ask_user_question_supports_other_and_cancel(self) -> None:
        registry = QuestionRegistry()
        registry.register("q2")
        self.bus.emit(AskUserQuestion(
            question_id="q2",
            question="补充？",
            options=[{"label": "A", "description": ""}, {"label": "B", "description": ""}],
        ))
        self.assertTrue(self.renderer.try_answer_pending(
            conversation=self.conversation,
            text="其他: 自定义答案",
            registry=registry,
        ))
        slot = registry.wait_for_answer("q2", timeout=0.01)
        self.assertEqual(slot.selected_labels, ["Other"])
        self.assertEqual(slot.other_text, "自定义答案")
        registry.discard("q2")

        registry.register("q3")
        self.bus.emit(AskUserQuestion(
            question_id="q3",
            question="取消？",
            options=[{"label": "A", "description": ""}, {"label": "B", "description": ""}],
        ))
        self.assertTrue(self.renderer.try_answer_pending(
            conversation=self.conversation,
            text="取消",
            registry=registry,
        ))
        slot = registry.wait_for_answer("q3", timeout=0.01)
        self.assertTrue(slot.cancelled)
        registry.discard("q3")

    def test_answered_event_confirms_and_clears_pending(self) -> None:
        registry = QuestionRegistry()
        registry.register("q4")
        self.bus.emit(AskUserQuestion(
            question_id="q4",
            question="选？",
            options=[{"label": "A", "description": ""}, {"label": "B", "description": ""}],
        ))
        self.bus.emit(AskUserQuestionAnswered(question_id="q4", selected_labels=["A"]))
        self.assertIn("已选择：A", self.sent[-1].segments[0].text)
        self.assertFalse(self.renderer.has_pending_question(self.conversation))

    def test_send_message_asset_tool_complete_renders_file_segment(self) -> None:
        payload = {
            "queued": True,
            "kind": "sticker",
            "path": "C:/tmp/a.png",
            "file_name": "a.png",
            "caption": "给你一个表情",
        }
        self.bus.emit(ToolComplete(
            call_id="c1",
            name="send_message_asset",
            result=json.dumps(payload, ensure_ascii=False),
            duration_seconds=0.1,
        ))
        msg = self.sent[-1]
        self.assertEqual([seg.kind for seg in msg.segments], ["text", "sticker"])
        self.assertEqual(Path(msg.segments[1].path), Path("C:/tmp/a.png"))

    def test_contextvar_routes_event_to_current_conversation(self) -> None:
        """并发 QQ 会话里，事件应优先按 ContextVar 路由，而不是串到 active 会话。"""

        other = ConversationKey(platform="qq", kind="private", id="20002")
        token = set_current_platform_conversation(other)
        try:
            self.bus.emit(Done(final_answer="发给私聊", rounds_used=1))
        finally:
            reset_current_platform_conversation(token)

        self.assertEqual(self.sent[-1].conversation, other)
        self.assertEqual(self.sent[-1].segments[0].text, "发给私聊")


class TestPlatformTodoIsolation(unittest.TestCase):
    def test_todo_tool_uses_independent_store_per_platform_conversation(self) -> None:
        """TodoTool 是全局工具实例，通讯平台模式下必须按群/好友隔离任务列表。"""

        tool = TodoTool()
        group = ConversationKey(platform="qq", kind="group", id="10001")
        private = ConversationKey(platform="qq", kind="private", id="20002")

        token = set_current_platform_conversation(group)
        try:
            group_result = json.loads(tool.run({
                "todos": [{"id": "1", "content": "群里的任务", "status": "pending"}],
            }))
        finally:
            reset_current_platform_conversation(token)

        token = set_current_platform_conversation(private)
        try:
            private_result = json.loads(tool.run({
                "todos": [{"id": "1", "content": "私聊任务", "status": "pending"}],
            }))
            private_read = json.loads(tool.run({}))
        finally:
            reset_current_platform_conversation(token)

        token = set_current_platform_conversation(group)
        try:
            group_read = json.loads(tool.run({}))
        finally:
            reset_current_platform_conversation(token)

        default_read = json.loads(tool.run({}))

        self.assertEqual(group_result["todos"][0]["content"], "群里的任务")
        self.assertEqual(private_result["todos"][0]["content"], "私聊任务")
        self.assertEqual(group_read["todos"][0]["content"], "群里的任务")
        self.assertEqual(private_read["todos"][0]["content"], "私聊任务")
        self.assertEqual(default_read["todos"], [])


class TestSendMessageAssetTool(unittest.TestCase):
    def test_sticker_name_and_arbitrary_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stickers = root / "assets" / "stickers"
            stickers.mkdir(parents=True)
            sticker = stickers / "happy.png"
            sticker.write_bytes(b"png")
            arbitrary = root / "report.txt"
            arbitrary.write_text("hello", encoding="utf-8")

            tool = SendMessageAssetTool(project_root=root, sticker_dir=stickers)
            sticker_result = json.loads(tool.run({"kind": "sticker", "sticker_name": "happy"}))
            file_result = json.loads(tool.run({"kind": "file", "path": str(arbitrary)}))

            self.assertTrue(sticker_result["queued"])
            self.assertEqual(sticker_result["kind"], "sticker")
            self.assertEqual(sticker_result["file_name"], "happy.png")
            self.assertTrue(file_result["queued"])
            self.assertEqual(file_result["path"], str(arbitrary.resolve()))

    def test_rejects_missing_directory_and_oversized_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            directory = root / "dir"
            directory.mkdir()
            big = root / "big.bin"
            big.write_bytes(b"x" * 20)
            tool = SendMessageAssetTool(project_root=root)

            directory_result = json.loads(tool.run({"kind": "file", "path": str(directory)}))
            self.assertFalse(directory_result["queued"])
            self.assertIn("不是普通文件", directory_result["error"])

            with patch.dict(os.environ, {"CBAGENT_OUTBOUND_FILE_MAX_MB": "0.000001"}):
                big_result = json.loads(tool.run({"kind": "file", "path": str(big)}))
            self.assertFalse(big_result["queued"])
            self.assertIn("超过限制", big_result["error"])


if __name__ == "__main__":
    unittest.main()
