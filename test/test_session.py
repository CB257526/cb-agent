"""AgentSession 单测。

不依赖真实 OpenAI API；用 fake LLM / fake registry 验流程和事件。
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

# 让单测从任意 cwd 都能 import（test/ 下直接跑、cb-agent/ 下跑都行）
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from agent.cancel import get_current_cancel_token
from agent.compaction import (
    COMPACTION_SUMMARY_KIND,
    SUMMARY_PREFIX,
    estimate_message_tokens,
    make_summary_message,
)
from agent.event_bus import EventBus, collect_all
from agent.llm_errors import LLMContextOverflowError
from agent.events import (
    Cancelled, ContextWindowUpdated, Done, Error, ReasoningDelta, RoundEnd, RoundStart,
    TextDelta, TokenUsage,
)
from agent.executor import ToolExecutor
from agent.session import AgentSession
from agent.work_context import LocalSessionStore
from constant.llm.constant_llm import ConstantLLM
from core.message import Message, MessageRole


# ========== fakes ==========


class FakeLLM:
    """模拟 CbAgentsLLM.think 行为。

    用法：构造时给一组 results；每次 think 按顺序返回。
    每次 think 时会按需 emit 流式事件。
    """

    def __init__(self, results: List[Any], emit_text: bool = False, emit_reasoning: bool = False) -> None:
        self.results = list(results)
        self.is_Function_Calling = True
        self.model = "fake"
        self.emit_text = emit_text
        self.emit_reasoning = emit_reasoning
        self.calls: List[Dict[str, Any]] = []
        self.compact_calls: List[Dict[str, Any]] = []
        self.max_output_tokens = 4096
        self.output_token_param = "max_tokens"
        owner = self

        class _Completions:
            def create(self, **kwargs):
                owner.compact_calls.append(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(
                        message=SimpleNamespace(content="测试交接摘要")
                    )]
                )

        self.client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))

    def _apply_output_token_limit(self, request_kwargs):
        request_kwargs[self.output_token_param] = self.max_output_tokens

    def think(self, messages, tools=None, event_bus=None, round_idx=0, cancel_event=None):
        self.calls.append({
            "messages": list(messages),
            "tools": tools,
            "round_idx": round_idx,
            "had_bus": event_bus is not None,
        })
        if not self.results:
            raise AssertionError("FakeLLM.think 调用次数超出 results")
        result = self.results.pop(0)
        # 允许测试把结构化 provider 异常放进结果队列，覆盖真实 think 的抛错路径。
        if isinstance(result, BaseException):
            raise result
        # 模拟流式 emit：reasoning 先于 text
        if event_bus is not None:
            if self.emit_reasoning:
                event_bus.emit(ReasoningDelta(delta="思考中…", accumulated="思考中…", round_idx=round_idx))
            answer = result.get("answer", "") if isinstance(result, dict) else ""
            if self.emit_text and answer:
                event_bus.emit(TextDelta(delta=answer, accumulated=answer, round_idx=round_idx))
        return result


def _tc(name: str, args: str = "{}", call_id: str = "") -> dict:
    return {
        "id": call_id or f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


# ========== AgentSession ==========


# 能力覆盖类 env 键：ConstantLLM 会优先读这 4 个 env 覆盖 llm_dict。
# cb_agents.py 顶部的 load_dotenv() 在 import 时就把用户本地 .env 里的
# 这些值（如 MAX_TOKENS=1024K、IMAGE_ABILITY=False）灌进了 os.environ，
# 会盖掉测试用 llm_dict monkeypatch 的窗口/视觉能力。测试期间清掉它们，
# 让用例只受 llm_dict monkeypatch 控制。
_CAPABILITY_ENV_KEYS = ("IS_TOOL", "IS_REASONING", "MAX_TOKENS", "MAX_OUTPUT_TOKENS", "IMAGE_ABILITY")


def _isolate_capability_env(test_case: unittest.TestCase) -> None:
    """删除能力覆盖类 env 变量，并在测试结束后恢复原值。"""
    saved = {k: os.environ.pop(k, None) for k in _CAPABILITY_ENV_KEYS}

    def _restore() -> None:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    test_case.addCleanup(_restore)


class TestAgentSessionBasic(unittest.TestCase):
    def setUp(self):
        # 先做 env 隔离，避免用户 .env 的能力覆盖值干扰 llm_dict monkeypatch。
        _isolate_capability_env(self)
        self.bus = EventBus()
        self.events = collect_all(self.bus)
        self.registry = MagicMock()
        self.registry.execute_tool = MagicMock(return_value="{}")
        self.registry.get_tools_description_openai_schema = MagicMock(return_value=[])
        self.registry.get_tools_description = MagicMock(return_value="")
        self.registry.list_tools = MagicMock(return_value=["bash", "file_read"])
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)

    def _make_session(self, llm: FakeLLM, **kwargs) -> AgentSession:
        return AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, memory_loader=None, skill_manager=None,
            ctx_enabled=False, **kwargs,
        )

    def test_chat_no_tool_calls_returns_answer(self):
        llm = FakeLLM([{"answer": "你好", "tool_calls": []}])
        s = self._make_session(llm)
        ans = s.chat("hi")
        self.assertEqual(ans, "你好")
        self.assertEqual(len(llm.calls), 1)

    def test_chat_emits_round_start_end_and_done(self):
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
        s = self._make_session(llm)
        s.chat("q")

        starts = [e for e in self.events if isinstance(e, RoundStart)]
        ends = [e for e in self.events if isinstance(e, RoundEnd)]
        dones = [e for e in self.events if isinstance(e, Done)]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertTrue(ends[0].final)
        self.assertEqual(len(dones), 1)
        self.assertEqual(dones[0].final_answer, "ok")
        self.assertEqual(dones[0].rounds_used, 1)
        self.assertIsInstance(dones[0].context_window, dict)
        self.assertGreater(dones[0].context_window["used_tokens"], 0)
        self.assertEqual(dones[0].context_window["scope"], "next_request_baseline")

    def test_context_window_uses_dynamic_limits_and_full_window_denominator(self):
        """Context 分母使用完整窗口，并单独暴露动态 soft/hard limit。"""
        original = ConstantLLM.llm_dict.get("fake")
        ConstantLLM.llm_dict["fake"] = {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 1000,
            "image_ability": False,
        }
        try:
            llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
            s = self._make_session(llm)
            s.chat("q")
            usage = s.context_window_usage()
            self.assertEqual(usage["full_window_tokens"], 1000)
            self.assertEqual(usage["max_tokens"], 1000)
            self.assertEqual(usage["max_output_tokens"], 200)
            self.assertEqual(usage["hard_limit_tokens"], 800)
            self.assertEqual(usage["soft_limit_tokens"], 640)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_provider_usage_updates_context_calibration_and_session_usage(self):
        """provider 实际 usage 覆盖展示值，并持久化当前会话累计量。"""
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            s = self._make_session(
                FakeLLM([]),
                session_store=store,
            )
            s._request_token_estimates[1] = 120

            self.bus.emit(TokenUsage(
                prompt_tokens=100,
                completion_tokens=5,
                total_tokens=105,
                cached_prompt_tokens=80,
                model="fake",
                round_idx=1,
            ))

            usage = s.current_session_payload()["usage"]
            self.assertEqual(usage["prompt_tokens"], 100)
            self.assertEqual(usage["cached_prompt_tokens"], 80)
            self.assertEqual(usage["completion_tokens"], 5)
            updates = [event for event in self.events if isinstance(event, ContextWindowUpdated)]
            self.assertEqual(updates[-1].context_window["used_tokens"], 100)
            self.assertEqual(updates[-1].context_window["raw_estimated_tokens"], 120)
            self.assertEqual(updates[-1].context_window["source"], "provider")
            self.assertAlmostEqual(updates[-1].context_window["calibration_ratio"], 100 / 120, places=3)

    def test_image_message_remains_exact_prefix_across_user_turns(self):
        """普通 append 流程不会在下一用户回合改写已经发送过的图片消息。"""
        original = ConstantLLM.llm_dict.get("fake")
        ConstantLLM.llm_dict["fake"] = {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 100000,
            "image_ability": True,
        }
        try:
            with tempfile.TemporaryDirectory() as td:
                image = Path(td) / "shot.png"
                image.write_bytes(b"image bytes")
                llm = FakeLLM([
                    {"answer": "看到了", "tool_calls": []},
                    {"answer": "继续", "tool_calls": []},
                ])
                s = self._make_session(llm)

                s.chat("图里有什么", attachments=[{"path": str(image), "source": "direct"}])
                s.chat("继续说明")

            first_messages = llm.calls[0]["messages"]
            last_user = first_messages[-1]
            self.assertIsInstance(last_user["content"], list)
            image_parts = [p for p in last_user["content"] if p.get("type") == "image_url"]
            self.assertEqual(len(image_parts), 1)
            self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))

            second_messages = llm.calls[1]["messages"]
            self.assertEqual(first_messages, second_messages[:len(first_messages)])
            history_dump = json.dumps([m.to_dict() for m in s.history], ensure_ascii=False)
            self.assertIn("data:image/png;base64,", history_dump)

        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_text_model_image_attachment_uses_ocr_text(self):
        """纯文本主模型不能吃图片时，图片会先转换成文本块再进入请求和 history。"""
        original = ConstantLLM.llm_dict.get("fake")
        ConstantLLM.llm_dict["fake"] = {
            "is_tool": True,
            "is_reasoning": False,
            "max_tokens": 100000,
            "image_ability": False,
        }

        class FakeProcessor:
            def process_image(self, file_path: str) -> Dict[str, str]:
                return {"text": f"OCR 摘要: {Path(file_path).name}"}

            def process_audio(self, file_path: str) -> Dict[str, str]:
                return {"text": f"ASR 摘要: {Path(file_path).name}"}

        try:
            with tempfile.TemporaryDirectory() as td:
                image = Path(td) / "shot.png"
                image.write_bytes(b"image bytes")
                llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
                s = self._make_session(llm)

                with patch("agent.multimodal_input.MultimodalProcessor", return_value=FakeProcessor()):
                    s.chat("读图", attachments=[{"path": str(image)}])

            request_dump = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
            history_dump = json.dumps([m.to_dict() for m in s.history], ensure_ascii=False)
            self.assertIn("OCR 摘要: shot.png", request_dump)
            self.assertIn("OCR 摘要: shot.png", history_dump)
            self.assertNotIn("data:image", request_dump)
            self.assertNotIn("data:image", history_dump)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_request_token_estimate_omits_data_uri_payload(self):
        """自动 compact 的请求估算只统计脱敏占位符，不把 base64 当长期文本。"""
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
        s = self._make_session(llm)
        messages = [{
            "role": "user",
            "content": [{
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64," + ("x" * 100_000)},
            }],
        }]

        tokens = s._estimate_request_tokens(messages, [])

        self.assertLess(tokens, 1000)

    def test_chat_with_tool_call_runs_two_rounds(self):
        # 第 1 轮：模型让调 file_read；第 2 轮：模型给最终答案
        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("file_read", '{"path":"a.txt"}')]},
            {"answer": "看到了 abc", "tool_calls": []},
        ])
        self.registry.execute_tool = MagicMock(return_value='{"content":"abc"}')
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
        )
        ans = s.chat("读 a.txt")
        self.assertEqual(ans, "看到了 abc")
        self.assertEqual(len(llm.calls), 2)
        # 第 2 次 think 的 messages 里应有 tool 消息
        round2_msgs = llm.calls[1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" for m in round2_msgs))

    def test_cancelled_tool_result_stops_before_next_llm_round(self):
        """Bash 权限拒绝会设置取消令牌，session 应在下一轮 think 前直接收束。"""

        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("bash", '{"command":"python build.py"}')]},
            {"answer": "不应该进入第二轮", "tool_calls": []},
        ])

        def deny_bash(_name, _args):  # noqa: ANN001
            token = get_current_cancel_token()
            self.assertIsNotNone(token)
            assert token is not None
            token.cancel()
            return json.dumps({
                "stdout": "",
                "stderr": "[权限拒绝] 用户拒绝执行 bash",
                "exit_code": 126,
                "is_error": True,
                "session_cancelled": True,
            }, ensure_ascii=False)

        self.registry.execute_tool = MagicMock(side_effect=deny_bash)
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
        )

        answer = s.chat("运行构建")

        self.assertEqual(answer, "")
        self.assertEqual(len(llm.calls), 1)
        self.assertTrue(any(isinstance(e, Cancelled) and e.where == "session" for e in self.events))
        dones = [e for e in self.events if isinstance(e, Done)]
        self.assertTrue(dones)
        self.assertTrue(dones[-1].cancelled)

    def test_tool_loop_keeps_raw_tool_result_for_next_round(self):
        """CC 模式:工具结果原样回灌进下一轮 messages,result_cap 持久化超大输出,
        但 tool_call_id 配对必须仍然合法。"""
        call_id = "call_loop_compress"
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "is_image_ability": False, "max_tokens": 5000,
            }
            llm = FakeLLM([
                {"answer": "", "tool_calls": [_tc(
                    "file_read", '{"path":"big.txt"}', call_id=call_id,
                )]},
                {"answer": "已基于工具结果继续", "tool_calls": []},
            ])
            huge_content = "X" * 200
            self.registry.execute_tool = MagicMock(return_value=json.dumps({
                "path": "big.txt",
                "mode": "all",
                "total_lines": 200,
                "returned_lines": 200,
                "truncated": False,
                "content": huge_content,
            }, ensure_ascii=False))
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )

            answer = s.chat("读 big.txt")

            self.assertEqual(answer, "已基于工具结果继续")
            self.assertEqual(len(llm.calls), 2)
            round2_msgs = llm.calls[1]["messages"]
            tool_msgs = [m for m in round2_msgs if m.get("role") == "tool"]
            self.assertEqual(len(tool_msgs), 1)
            self.assertEqual(tool_msgs[0].get("tool_call_id"), call_id)
            # 结果原样回灌（result_cap 不会触发，因为低于 10K token/40K bytes）
            self.assertIn(huge_content, tool_msgs[0].get("content", ""))
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_active_history_keeps_full_tool_call_pair(self):
        """active history 始终全量保留，工具调用链不得被消息数裁剪。

        构造:第一轮一个 file_read 工具调用 -> 第二轮收尾。第二轮请求里 tool 结果必须仍能在前文找到它的 assistant.tool_calls。
        """
        call_id = "call_orphan_cut"
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "is_image_ability": False, "max_tokens": 100000,
            }
            llm = FakeLLM([
                {"answer": "", "tool_calls": [_tc(
                    "file_read", '{"path":"a.txt"}', call_id=call_id,
                )]},
                {"answer": "第一轮完成", "tool_calls": []},
                {"answer": "第二轮完成", "tool_calls": []},
            ])
            self.registry.execute_tool = MagicMock(return_value=json.dumps({
                "path": "a.txt", "content": "hello",
            }, ensure_ascii=False))
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )

            s.chat("读 a.txt")
            # 第一轮后 history:user, assistant(tool_calls), tool, assistant(final)

            s.chat("继续")

            # 第二轮(第 3 次 think)的请求体里不应有任何孤儿 tool
            round_msgs = llm.calls[2]["messages"]
            seen_ids = set()
            for m in round_msgs:
                if m.get("role") == "assistant":
                    for tc in (m.get("tool_calls") or []):
                        seen_ids.add(tc.get("id"))
            for m in round_msgs:
                if m.get("role") == "tool":
                    self.assertIn(
                        m.get("tool_call_id"), seen_ids,
                        msg="发给 LLM 的 tool 消息必须能在前文找到声明它的 assistant.tool_calls",
                    )
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_tool_loop_freezes_system_and_tools_schema_for_current_turn(self):
        """注册表中途变化不能改写同一用户回合的请求外壳。"""

        llm = FakeLLM([
            {
                "answer": "",
                "tool_calls": [_tc("file_read", '{"path":"a.txt"}', call_id="call-a")],
            },
            {"answer": "完成", "tool_calls": []},
        ])
        schema_v1 = [{
            "type": "function",
            "function": {
                "name": "file_read",
                "description": "v1",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        schema_v2 = [*schema_v1, {
            "type": "function",
            "function": {
                "name": "new_tool",
                "description": "v2",
                "parameters": {"type": "object", "properties": {}},
            },
        }]
        schema_reads = iter([schema_v1])
        tool_reads = iter([["file_read"]])
        self.registry.get_tools_description_openai_schema.side_effect = (
            lambda: next(schema_reads, schema_v2)
        )
        self.registry.list_tools.side_effect = (
            lambda: next(tool_reads, ["new_tool", "file_read"])
        )

        def execute_and_change_registry(*_args, **_kwargs):
            return json.dumps({"path": "a.txt", "content": "ok"})

        self.registry.execute_tool = MagicMock(side_effect=execute_and_change_registry)
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        session = self._make_session(llm)
        session.chat("读取文件")

        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[0]["messages"][0], llm.calls[1]["messages"][0])
        self.assertEqual(llm.calls[0]["tools"], llm.calls[1]["tools"])
        self.assertNotIn("new_tool", str(llm.calls[1]["tools"]))

    def test_dynamic_context_counts_tool_call_arguments(self):
        """P1 回归:Context% 估算必须把纯 tool_calls 的 arguments 计入。

        重构后 history 里 assistant(tool_calls, content=None) 是大头(file_write
        的完整内容就藏在 arguments 里)。旧逻辑按"有正文才计入"会把它整段漏算,
        导致 Context% 系统性偏低。这里断言带大 arguments 的工具调用被计入估算。"""
        call_id = "call_ctx_count"
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "is_image_ability": False, "max_tokens": 100000,
            }
            big_args = json.dumps({"path": "x.py", "content": "Z" * 2000})
            llm = FakeLLM([
                {"answer": "", "tool_calls": [_tc(
                    "file_write", big_args, call_id=call_id,
                )]},
                {"answer": "写好了", "tool_calls": []},
            ])
            self.registry.execute_tool = MagicMock(return_value=json.dumps({
                "ok": True,
            }, ensure_ascii=False))
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )

            s.chat("写 x.py")
            text = s._dynamic_context_text()
            # arguments 里那 2000 个 Z 必须体现在估算文本里(不被漏算)
            self.assertIn("Z" * 100, text)
            usage = s.context_window_usage()
            self.assertGreater(usage["used_tokens"], 400)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_dynamic_context_uses_installed_replacement_history(self):
        """Context 估算只读取 compact 安装后的 replacement history。"""
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "is_image_ability": False, "max_tokens": 100000,
            }
            llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )
            from core.message import Message
            s._append_history([
                Message.create_user_message("早期问题 " + "A" * 3000),
                Message.create_assistant_message("早期回答 " + "B" * 3000),
            ], turn_id="old")
            text_before = s._dynamic_context_text()
            self.assertIn("A" * 100, text_before)

            # compact 会直接替换 active history，不再依赖 boundary 切片。
            s._replace_history([
                Message.create_user_message("新问题"),
                make_summary_message("摘要", reason="auto"),
            ], reason="test")

            text_after = s._dynamic_context_text()
            # 早期长消息已被切片排除,不再出现在估算文本里
            self.assertNotIn("A" * 100, text_after)
            self.assertIn("摘要", text_after)
            self.assertIn("新问题", text_after)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_replacement_history_is_not_message_count_trimmed(self):
        """验证 canonical history 按全量发送，不按消息数截断。"""
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "image_ability": False, "max_tokens": 100000,
            }
            llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )
            from core.message import Message
            # 构造 summary + 6 条尾部消息，应全部进入请求。
            for i in range(6):
                s._append_history(
                    [Message.create_user_message(f"tail-{i}")],
                    turn_id=f"turn-{i}",
                )
            s._append_history([make_summary_message("ANCHOR_SUMMARY", reason="auto")])

            dicts = s.history.provider_messages()

            # 应该返回完整 active history：6 条 tail + summary。
            self.assertEqual(len(dicts), 7)
            self.assertIn("ANCHOR_SUMMARY", str(dicts[-1].get("content")))
            joined = json.dumps(dicts, ensure_ascii=False)
            for i in range(6):
                self.assertIn(f"tail-{i}", joined)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_approved_plan_is_injected_once_without_history_duplication(self):
        """Approved plan 由 PlanState 每轮注入,不随历史 context_update 重复累积。"""
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "image_ability": False, "max_tokens": 100000,
            }
            unique = "UNIQUE_APPROVED_PLAN_STEP_42"
            plan = f"# Approved Plan\n\n- {unique}\n- keep implementing"
            llm = FakeLLM([
                {"answer": "first ok", "tool_calls": []},
                {"answer": "second ok", "tool_calls": []},
            ])

            with tempfile.TemporaryDirectory() as td:
                store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
                s = AgentSession(
                    llm=llm, registry=self.registry, executor=self.executor,
                    event_bus=self.bus, ctx_enabled=False, session_store=store,
                )
                s.plan_store.save_pending_plan(plan)
                s.approve_plan()
                self.assertEqual(
                    (s.history[-1].metadata or {}).get("kind"),
                    "plan_state",
                )

                s.chat("first")
                first_request = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
                self.assertEqual(first_request.count(unique), 1)

                history_dump = json.dumps([m.to_dict() for m in s.history], ensure_ascii=False)
                self.assertEqual(history_dump.count(unique), 1)

                s.chat("second")
                second_request = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
                self.assertEqual(second_request.count(unique), 1)
                self.assertIn(unique, s._dynamic_context_text())
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_preflight_auto_compact_when_full_request_exceeds_budget(self):
        """验证 preflight 只在完整请求超过安全上下文预算时触发 compact。

        场景：模型窗口 20000，默认输出预留 4000，动态 soft limit 为 12800。
        注入多条可装入 hard limit 的中等消息，合计超过 soft limit。

        预期行为：
        - chat 正常返回 "ok"（compact 成功释放空间后继续）
        - history 中出现 context_compaction handoff 消息
        - Done.auto_compact 非空
        - 触发原因是 "preflight_context_overflow"
        """
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "image_ability": False, "max_tokens": 20000,
            }
            llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )
            from core.message import Message

            # 多条中等 user/assistant 回合：合计超 soft limit，但单段不超 hard limit。
            # 单条超大用户消息会按无丢失策略直接失败，不再静默缩短正文。
            chunk = "word " * 2200
            for idx in range(4):
                s._append_history([
                    Message.create_user_message(f"turn-{idx} {chunk}"),
                    Message.create_assistant_message(f"reply-{idx} {chunk}"),
                ], turn_id=f"turn-{idx}")
            answer = s.chat("continue")

            self.assertEqual(answer, "ok")
            # 验证新的 handoff summary 已写入 history。
            self.assertTrue(any(
                (m.metadata or {}).get("kind") == COMPACTION_SUMMARY_KIND
                for m in s.history
            ))
            # 验证 Done 事件包含 auto_compact 信息
            dones = [e for e in self.events if isinstance(e, Done)]
            self.assertTrue(dones[-1].auto_compact)
            reasons = [item.get("reason") for item in dones[-1].auto_compact["events"]]
            self.assertIn("preflight_context_overflow", reasons)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_tool_call_blocks_when_full_window_overflows(self):
        """模型完整窗口被即将超过时,preflight blocking 阈值会拒绝继续。"""
        original = ConstantLLM.llm_dict.get("fake")
        ConstantLLM.llm_dict["fake"] = {
            "is_tool": True, "is_reasoning": False,
            "is_image_ability": False, "max_tokens": 700,
        }
        try:
            llm = FakeLLM([])  # 不应该被调用,blocking 早返回
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )
            answer = s.chat("X" * 5000)  # 超出 700 max_tokens
            self.assertIn("[上下文窗口已满]", answer)
            self.assertEqual(len(llm.calls), 0)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_tool_trace_persists_state_and_round2_sees_raw_tool_result(self):
        """CC 模式下 history 累积原始 tool_calls + tool_result; state.json
        提取结构化字段(files_seen 等)供下一轮使用。"""
        long_content = "abcdef" * 40
        llm = FakeLLM([
            {"answer": "", "tool_calls": [_tc("file_read", '{"path":"a.txt"}')]},
            {"answer": "看到了文件", "tool_calls": []},
            {"answer": "继续", "tool_calls": []},
        ])
        self.registry.execute_tool = MagicMock(return_value=json.dumps({
            "path": "a.txt",
            "mode": "head-100",
            "total_lines": 12,
            "returned_lines": 12,
            "truncated": False,
            "content": long_content,
        }, ensure_ascii=False))
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)

        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("读 a.txt")

            # CC 模式 history: user + assistant(tool_calls) + tool + final = 4
            self.assertEqual(len(s.history), 5)
            roles = [m.role.value if hasattr(m.role, "value") else str(m.role)
                     for m in s.history]
            self.assertEqual(roles, ["user", "user", "assistant", "tool", "assistant"])
            self.assertEqual((s.history[0].metadata or {}).get("kind"), "context_update")
            self.assertTrue(s.history[2].tool_calls)
            self.assertEqual(s.history[3].tool_call_id, s.history[2].tool_calls[0]["id"])

            # 第 2 轮请求里 tool_result 原文仍在
            round2_tool_msgs = llm.calls[1]["messages"]
            self.assertTrue(any(
                m.get("role") == "tool" and long_content in m.get("content", "")
                for m in round2_tool_msgs
            ))

            # state.json 已通过结构化字段提取记录 a.txt
            self.assertIn("a.txt", json.dumps(store.state, ensure_ascii=False))

            # canonical journal 落盘的是原始协议消息。
            journal = store.active_dir / "history.jsonl"
            raw_journal = journal.read_text(encoding="utf-8")
            self.assertIn("file_read", raw_journal)

            # 第 3 轮再问,模型仍然能在 history 里看到上一轮原始 tool_calls / tool_result
            s.chat("继续分析")
            next_turn_messages = llm.calls[2]["messages"]
            self.assertTrue(any(
                m.get("role") == "tool" and long_content in str(m.get("content", ""))
                for m in next_turn_messages
            ))

    def test_tool_loop_is_append_only_without_clearing_old_tool_results(self):
        """验证连续工具轮次只追加消息，不改写已经发送过的工具结果。"""
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "image_ability": False, "max_tokens": 100000,
            }
            # 10 轮工具调用加 1 轮最终回答，累计量足以覆盖旧压缩路径的触发场景。
            tool_rounds = [
                {"answer": "", "tool_calls": [_tc("file_read", "{}", call_id=f"call_{i}")]}
                for i in range(10)
            ]
            llm = FakeLLM(tool_rounds + [{"answer": "done", "tool_calls": []}])
            long_content = "word " * 1500
            self.registry.execute_tool = MagicMock(return_value=json.dumps({
                "content": long_content,
            }, ensure_ascii=False))
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
            )

            # 关闭回合结束后的正式 compact，避免它按设计替换跨轮历史，确保本用例
            # 只观察工具循环内部的追加行为。
            with patch.object(s, "_maybe_auto_compact_history", return_value=None):
                answer = s.chat("start")

            self.assertEqual(answer, "done")

            # 每次后续请求都必须保留此前请求中的完整消息内容，且只能在尾部增长。
            for previous_call, current_call in zip(llm.calls, llm.calls[1:]):
                previous_messages = previous_call["messages"]
                current_messages = current_call["messages"]
                self.assertEqual(
                    current_messages[:len(previous_messages)],
                    previous_messages,
                )

            final_request = llm.calls[-1]["messages"]
            tool_messages = [m for m in final_request if m.get("role") == "tool"]
            self.assertEqual(len(tool_messages), 10)
            self.assertTrue(all(long_content in str(m.get("content")) for m in tool_messages))
            self.assertFalse(any('"cleared": true' in str(m.get("content")) for m in tool_messages))

            # 工具循环结束后提交到 history 的结果也必须保持完整。
            self.assertTrue(any(
                (m.role.value if hasattr(m.role, "value") else str(m.role)) == "tool"
                and long_content in str(m.content)
                for m in s.history
            ))

            # Done 中不再产生工具循环局部压缩事件。
            dones = [e for e in self.events if isinstance(e, Done)]
            events = (dones[-1].auto_compact or {}).get("events", [])
            self.assertFalse(any(item.get("reason") == "tool_loop" for item in events))
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_mid_turn_does_not_summarize_the_only_active_turn(self):
        """只有当前活动回合时，soft-limit 检查不能压缩正在执行的工具现场。"""
        original = ConstantLLM.llm_dict.get("fake")
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        session_root = Path(temp_dir.name) / ".cbagent" / "sessions"
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True,
                "is_reasoning": False,
                "image_ability": False,
                # 小窗口仍需给静态 system、摘要指令和单条 10K 工具结果留出空间；
                # 15K 会让工具结果越过 soft limit，但仍可完整装入 compact hard limit。
                "max_tokens": 15000,
                "max_output_tokens": 2000,
            }
            llm = FakeLLM([
                {"answer": "", "tool_calls": [_tc("file_read", "{}", call_id="call_mid")]},
                {"answer": "done", "tool_calls": []},
            ])
            self.registry.execute_tool = MagicMock(return_value="word " * 30000)
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm,
                registry=self.registry,
                executor=self.executor,
                event_bus=self.bus,
                ctx_enabled=False,
                session_store=LocalSessionStore(session_root),
            )

            answer = s.chat("读取大文件后继续")

            self.assertEqual(answer, "done")
            self.assertEqual(len(llm.calls), 2)
            second_messages = llm.calls[1]["messages"]
            self.assertFalse(any(
                SUMMARY_PREFIX in str(message.get("content") or "")
                for message in second_messages
            ))
            dones = [event for event in self.events if isinstance(event, Done)]
            compact_events = (dones[-1].auto_compact or {}).get("events", [])
            self.assertFalse(any(event.get("reason") == "mid_turn" for event in compact_events))

            restored = AgentSession(
                llm=FakeLLM([]),
                registry=self.registry,
                executor=self.executor,
                event_bus=EventBus(),
                ctx_enabled=False,
                session_store=LocalSessionStore(session_root),
            )
            restored_text = [str(message.content or "") for message in restored.history]
            self.assertEqual(sum(text == "done" for text in restored_text), 1)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_provider_overflow_compacts_and_retries_same_round(self):
        """provider 明确报告超窗时，正式 compact 后应使用完整参数重试同一轮。"""
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            llm = FakeLLM([
                LLMContextOverflowError("context_length_exceeded"),
                {"answer": "重试成功", "tool_calls": []},
            ])
            session = AgentSession(
                llm=llm,
                registry=self.registry,
                executor=self.executor,
                event_bus=self.bus,
                ctx_enabled=False,
                session_store=store,
            )
            session._append_history([
                Message(role=MessageRole.USER, content="旧问题"),
                Message.create_assistant_message("旧回答"),
            ], turn_id="old-turn")

            with patch("agent.session.dynamic_retained_token_target", return_value=0):
                answer = session.chat("请在 provider 超窗后继续")

            self.assertEqual(answer, "重试成功")
            self.assertEqual(len(llm.calls), 2)
            self.assertTrue(any(
                SUMMARY_PREFIX in str(message.get("content") or "")
                for message in llm.calls[1]["messages"]
            ))
            dones = [event for event in self.events if isinstance(event, Done)]
            compact_events = (dones[-1].auto_compact or {}).get("events", [])
            self.assertTrue(any(event.get("reason") == "mid_turn" for event in compact_events))

    def test_session_store_restores_history_and_clear_deletes_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            llm = FakeLLM([
                {
                    "answer": "",
                    "tool_calls": [_tc("file_read", '{"path":"b.txt"}')],
                    "reasoning_content": "先读取文件",
                },
                {
                    "answer": "done",
                    "tool_calls": [],
                    "reasoning_content": "根据读取结果回答",
                },
            ])
            self.registry.execute_tool = MagicMock(return_value=json.dumps({
                "path": "b.txt",
                "mode": "head-100",
                "total_lines": 1,
                "returned_lines": 1,
                "content": "hello",
            }, ensure_ascii=False))
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("读 b.txt")
            active_dir = store.active_dir

            restored = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            # CC 模式 history: user + assistant(tool_calls) + tool + final = 4
            self.assertEqual(len(restored.history), 5)
            roles = [m.role.value if hasattr(m.role, "value") else str(m.role)
                     for m in restored.history]
            self.assertEqual(roles, ["user", "user", "assistant", "tool", "assistant"])
            self.assertEqual((restored.history[0].metadata or {}).get("kind"), "context_update")
            self.assertEqual(str(restored.history[-1].content), "done")
            assistant_messages = [
                message for message in restored.history
                if (message.role.value if hasattr(message.role, "value") else str(message.role)) == "assistant"
            ]
            self.assertEqual(assistant_messages[0].reasoning_content, "先读取文件")
            self.assertEqual(assistant_messages[1].reasoning_content, "根据读取结果回答")

            restored.clear_history()
            self.assertEqual(restored.history, [])
            self.assertFalse(active_dir.exists())
            self.assertFalse((root / "index.json").exists())

    def test_session_store_restores_active_turn_completed_tool_checkpoint(self):
        """没有 v4 journal 时，旧 active-turn 只迁移一次。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            call = _tc("file_read", '{"path":"active.txt"}', call_id="call_active")
            events = [
                {
                    "type": "turn_started",
                    "turn_id": "legacy-turn",
                    "user_query": "恢复工具检查点",
                    "user_payload": {"role": "user", "content": "恢复工具检查点"},
                },
                {
                    "type": "assistant_tool_calls",
                    "round_idx": 1,
                    "assistant_payload": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [call],
                    },
                },
                {
                    "type": "tool_terminal",
                    "round_idx": 1,
                    "tool_call_id": "call_active",
                    "tool_payload": {
                        "role": "tool",
                        "tool_call_id": "call_active",
                        "tool_name": "file_read",
                        "content": '{"content":"abc"}',
                    },
                },
            ]
            (store.active_dir / "active_turn.jsonl").write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8",
            )

            restored = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )

            exported = restored.export_history()
            exported_text = "\n".join(item["content"] for item in exported)
            self.assertIn("恢复工具检查点", exported_text)
            self.assertIn("【工具完成】file_read", exported_text)
            self.assertTrue(any(item.get("tool", {}).get("call_id") == "call_active" for item in exported))

            sliced = restored.history.provider_messages()
            roles = [m.get("role") for m in sliced]
            self.assertEqual(roles, ["user", "assistant", "tool", "user"])
            self.assertEqual(sliced[1]["tool_calls"][0]["id"], "call_active")
            self.assertEqual(sliced[2]["tool_call_id"], "call_active")
            self.assertTrue((store.active_dir / "history.jsonl").exists())

    def test_canonical_history_survives_continue_and_second_restart(self):
        """v4 journal 连续重启时不会重复或漏掉已完成回合。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            first = AgentSession(
                llm=FakeLLM([{"answer": "第一轮完成", "tool_calls": []}]),
                registry=self.registry,
                executor=self.executor,
                event_bus=self.bus,
                ctx_enabled=False,
                session_store=store,
            )
            first.chat("第一轮问题")

            resumed = AgentSession(
                llm=FakeLLM([{"answer": "继续处理完成", "tool_calls": []}]),
                registry=self.registry,
                executor=self.executor,
                event_bus=self.bus,
                ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            resumed.chat("继续")

            restarted = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            visible = [
                message for message in restarted.history
                if (message.metadata or {}).get("kind") != "context_update"
            ]
            text = "\n".join(str(message.content) for message in visible)

            self.assertEqual(text.count("第一轮问题"), 1)
            self.assertEqual(text.count("第一轮完成"), 1)
            self.assertEqual(text.count("继续"), 2)  # 用户输入与最终回答各出现一次。
            self.assertIn("继续处理完成", text)

    def test_completed_tool_checkpoint_survives_later_tool_process_exit(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            tool_calls = [
                _tc("file_read", '{"path":"first.txt"}', call_id="call_first"),
                _tc("bash", '{"command":"sleep 10"}', call_id="call_second"),
            ]
            llm = FakeLLM([{"answer": "", "tool_calls": tool_calls}])

            def run_tool(name: str, args: Dict[str, Any]) -> str:
                if name == "file_read":
                    return json.dumps({"path": args.get("path"), "content": "abc"}, ensure_ascii=False)
                raise KeyboardInterrupt()

            self.registry.execute_tool = MagicMock(side_effect=run_tool)
            self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )

            with self.assertRaises(KeyboardInterrupt):
                s.chat("先读文件再跑长命令")

            restarted = AgentSession(
                llm=FakeLLM([]),
                registry=self.registry,
                executor=self.executor,
                event_bus=EventBus(),
                ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            restored = list(restarted.history)
            visible = [
                m for m in restored
                if (m.metadata or {}).get("kind") != "context_update"
            ]

            self.assertEqual(
                [m.role.value if hasattr(m.role, "value") else str(m.role) for m in visible],
                ["user", "assistant", "tool", "tool"],
            )
            self.assertEqual(
                [tc["id"] for tc in visible[1].tool_calls],
                ["call_first", "call_second"],
            )
            self.assertEqual(visible[2].tool_call_id, "call_first")
            self.assertEqual(json.loads(str(visible[2].content))["content"], "abc")
            self.assertTrue(json.loads(str(visible[3].content))["recovered"])

    def test_final_answer_survives_state_commit_interruption(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            llm = FakeLLM([{
                "answer": "已经展示给用户的回答",
                "tool_calls": [],
                "reasoning_content": "最终思考",
            }], emit_text=True)
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            store.commit_turn_state = MagicMock(side_effect=KeyboardInterrupt())

            with self.assertRaises(KeyboardInterrupt):
                s.chat("需要可靠恢复的回答")

            restored = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            visible = [
                message for message in restored.history
                if (message.metadata or {}).get("kind")
                not in {"context_update"}
            ]

            self.assertEqual(
                [m.role.value if hasattr(m.role, "value") else str(m.role) for m in visible],
                ["user", "assistant"],
            )
            self.assertEqual(visible[-1].content, "已经展示给用户的回答")
            self.assertEqual(visible[-1].reasoning_content, "最终思考")

    def test_canonical_history_is_committed_before_memory_writeback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            s = AgentSession(
                llm=FakeLLM([{"answer": "先提交的回答", "tool_calls": []}]),
                registry=self.registry,
                executor=self.executor,
                event_bus=self.bus,
                ctx_enabled=False,
                session_store=store,
            )
            s._auto_update_memory_and_knowledge = MagicMock(side_effect=KeyboardInterrupt())

            with self.assertRaises(KeyboardInterrupt):
                s.chat("提交顺序")

            self.assertTrue((store.active_dir / "history.jsonl").exists())
            restored = AgentSession(
                llm=FakeLLM([]),
                registry=self.registry,
                executor=self.executor,
                event_bus=EventBus(),
                ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            restored_text = "\n".join(str(message.content) for message in restored.history)
            self.assertEqual(restored_text.count("提交顺序"), 1)
            self.assertEqual(restored_text.count("先提交的回答"), 1)

    def test_active_tool_error_is_exported_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            call = _tc("file_read", call_id="call_error")
            active = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            active._append_history([
                Message(role=MessageRole.USER, content="恢复失败工具"),
                Message.create_assistant_message(tool_calls=[call]),
            ], turn_id="turn-error")
            active._history_journal.checkpoint_tool_result(
                active.history,
                Message.create_tool_message(
                    "call_error",
                    "file_read",
                    '{"error":"denied"}',
                    is_error=True,
                ),
                turn_id="turn-error",
            )

            restored = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            tool_payload = next(item for item in restored.export_history() if item.get("tool"))

            self.assertIn("【工具失败】file_read", tool_payload["content"])
            self.assertTrue(tool_payload["tool"]["is_error"])

    def test_agent_session_create_and_switch_keeps_histories_isolated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            first_id = store.active_session_id
            llm = FakeLLM([
                {"answer": "第一会话回答", "tool_calls": []},
                {"answer": "第二会话回答", "tool_calls": []},
            ])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("第一会话问题")
            self.assertEqual(len(s.history), 3)
            self.assertEqual(len(s.export_history()), 2)

            created = s.create_session()
            second_id = created["session"]["session_id"]
            self.assertNotEqual(first_id, second_id)
            self.assertEqual(s.history, [])
            s.chat("第二会话问题")

            payload = s.switch_session(first_id)  # type: ignore[arg-type]
            self.assertEqual(payload["session"]["session_id"], first_id)
            restored = "\n".join(item["content"] for item in payload["history"])
            self.assertIn("第一会话问题", restored)
            self.assertIn("第一会话回答", restored)
            self.assertNotIn("第二会话问题", restored)
            self.assertEqual(store.active_session_id, first_id)

    def test_compact_context_replaces_history_and_retains_latest_turn(self):
        """compact_context 替换 active history，并保留最新完整回合。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            llm = FakeLLM([
                {"answer": "旧回答一", "tool_calls": []},
                {"answer": "旧回答二", "tool_calls": []},
                {"answer": "后续回答", "tool_calls": []},
            ])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("旧问题一")
            s.chat("旧问题二")
            # 首轮可能提交 1 条 persistent context_update；第二轮 section 稳定时
            # 不再重复提交。期望为 context_update? + 2*user + 2*assistant。
            self.assertIn(len(s.history), {4, 5, 6})
            self.assertGreaterEqual(len(s.export_history()), 4)
            history_before_compact = len(s.history)

            # 直接以最新完整回合的真实估算值作为保留预算，避免测试结果依赖
            # tiktoken 在线编码器或离线字符估算的差异。
            latest_turn_tokens = estimate_message_tokens(s.history[-2:])
            with patch(
                "agent.session.dynamic_retained_token_target",
                return_value=latest_turn_tokens,
            ):
                payload = s.compact_context()
            self.assertEqual(payload["before_messages"], history_before_compact)
            self.assertEqual(payload["after_messages"], 3)
            self.assertTrue(payload["persisted"])
            self.assertIn(SUMMARY_PREFIX, payload["summary"])
            # replacement history 末尾是 handoff summary，前面是最新回合首尾消息。
            self.assertEqual(
                (s.history[-1].metadata or {}).get("kind"),
                COMPACTION_SUMMARY_KIND,
            )
            self.assertIn("旧问题二", str(s.history[0].content))
            self.assertIn("旧回答二", str(s.history[1].content))
            self.assertTrue((store.active_dir / "history.jsonl").exists())
            self.assertFalse((store.active_dir / "compact.json").exists())
            self.assertFalse((store.active_dir / "transcript.jsonl").exists())

            s.chat("继续")
            next_turn_messages = llm.calls[2]["messages"]
            context_text = "\n".join(str(m.get("content", "")) for m in next_turn_messages)
            self.assertIn(SUMMARY_PREFIX, context_text)
            # replacement 之外的旧 user/assistant 不再作为独立条目出现在请求里。
            raw_user_assistant = [
                m for m in next_turn_messages
                if m.get("role") in {"user", "assistant"}
            ]
            # 旧回答一不再作为独立 assistant 消息保留。
            self.assertFalse(any(
                m.get("role") == "assistant" and m.get("content") == "旧回答一"
                for m in raw_user_assistant
            ))
            self.assertTrue(any(
                m.get("role") == "assistant" and m.get("content") == "旧回答二"
                for m in raw_user_assistant
            ))

    def test_compact_context_does_not_mutate_history_when_persist_fails(self):
        """compact 快照落盘失败时，内存 history 仍保持原样。"""
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            llm = FakeLLM([
                {"answer": "旧回答一", "tool_calls": []},
                {"answer": "旧回答二", "tool_calls": []},
            ])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            s.chat("旧问题一")
            s.chat("旧问题二")
            before = s.export_history()

            generation = s.history.generation
            with (
                patch("agent.session.dynamic_retained_token_target", return_value=20),
                patch.object(
                    s._history_journal,
                    "_append_event",
                    side_effect=OSError("disk full"),
                ),
            ):
                with self.assertRaises(OSError):
                    s.compact_context()

            self.assertEqual(s.export_history(), before)
            self.assertEqual(s.history.generation, generation)

    def test_compact_context_resets_memory_loader_cache(self):
        """手动 compact 完成 replacement history 后清理 MemoryLoader cache。"""
        class FakeMemoryLoader:
            def __init__(self):
                self.reasons: List[str] = []

            def reset_cache(self, reason: str = "") -> None:
                self.reasons.append(reason)

        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            loader = FakeMemoryLoader()
            llm = FakeLLM([
                {"answer": "旧回答一", "tool_calls": []},
                {"answer": "旧回答二", "tool_calls": []},
            ])
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, memory_loader=loader, ctx_enabled=False,
                session_store=store,
            )
            s.chat("旧问题一")
            s.chat("旧问题二")

            with patch("agent.session.dynamic_retained_token_target", return_value=20):
                s.compact_context()

            self.assertIn("user_compact", loader.reasons)

    def test_chat_prompt_keeps_runtime_context_out_of_first_system_message(self):
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
        s = self._make_session(llm)

        s.chat("hello")

        messages = llm.calls[0]["messages"]
        self.assertEqual(messages[0]["role"], "system")
        self.assertNotIn("# Current date", messages[0]["content"])
        self.assertNotIn("# Environment", messages[0]["content"])
        self.assertNotIn("Available tools:", messages[0]["content"])

        context_messages = [
            m for m in messages
            if m.get("role") == "user" and "<context-update>" in str(m.get("content", ""))
        ]
        self.assertEqual(len(context_messages), 1)
        self.assertIn("# Current date", context_messages[0]["content"])
        self.assertIn("# Environment", context_messages[0]["content"])
        self.assertIn("Available tools: bash, file_read.", context_messages[0]["content"])

        exported = s.export_history()
        self.assertFalse(any(item.get("kind") == "context_update" for item in exported))

    def test_second_turn_reuses_prior_context_update_as_committed_history_prefix(self):
        llm = FakeLLM([
            {"answer": "first answer", "tool_calls": []},
            {"answer": "second answer", "tool_calls": []},
        ])
        s = self._make_session(llm)

        s.chat("first question")
        first_request = llm.calls[0]["messages"]
        s.chat("second question")
        second_request = llm.calls[1]["messages"]

        self.assertEqual(second_request[:len(first_request)], first_request)
        self.assertEqual(second_request[3]["role"], "assistant")
        self.assertEqual(second_request[3]["content"], "first answer")
        self.assertEqual(second_request[-1]["role"], "user")
        self.assertEqual(second_request[-1]["content"], "second question")
        self.assertEqual(llm.calls[1]["tools"], llm.calls[0]["tools"])

    def test_tools_schema_is_sorted_before_every_request(self):
        llm = FakeLLM([{"answer": "ok", "tool_calls": []}])
        self.registry.get_tools_description_openai_schema = MagicMock(return_value=[
            {"type": "function", "function": {"name": "z_tool", "parameters": {}}},
            {"type": "function", "function": {"name": "a_tool", "parameters": {}}},
        ])
        s = self._make_session(llm)

        s.chat("hello")

        names = [item["function"]["name"] for item in llm.calls[0]["tools"]]
        self.assertEqual(names, ["a_tool", "z_tool"])

    def test_chat_history_appended_correctly(self):
        llm = FakeLLM([{"answer": "好的", "tool_calls": []}])
        s = self._make_session(llm)
        s.chat("hello")
        self.assertEqual(len(s.history), 3)
        self.assertEqual(len(s.export_history()), 2)
        # user + assistant
        roles = [m.role.value if hasattr(m.role, "value") else str(m.role) for m in s.history]
        self.assertIn("user", roles)
        self.assertIn("assistant", roles)

    def test_chat_history_empty_when_no_answer(self):
        # 模型没给 answer 也没给 tool_calls 但走到了非预期分支
        llm = FakeLLM([{"answer": "", "tool_calls": []}])
        s = self._make_session(llm)
        ans = s.chat("test")
        self.assertEqual(ans, "")
        # 完整 provider 响应即使文本为空，也属于不可变 assistant 协议项。
        self.assertEqual(len(s.history), 3)
        self.assertEqual(len(s.export_history()), 2)

    def test_clear_history(self):
        llm = FakeLLM([{"answer": "a", "tool_calls": []}])
        s = self._make_session(llm)
        s.chat("q")
        self.assertEqual(len(s.history), 3)
        s.clear_history()
        self.assertEqual(len(s.history), 0)

    def test_max_rounds_emits_error(self):
        # 永远让模型继续要工具
        infinite = [
            {"answer": "", "tool_calls": [_tc("file_read")]}
            for _ in range(AgentSession.MAX_TOOL_ROUNDS + 1)
        ]
        llm = FakeLLM(infinite)
        self.registry.execute_tool = MagicMock(return_value="{}")
        self.executor = ToolExecutor(self.registry.execute_tool, self.bus)
        s = AgentSession(
            llm=llm, registry=self.registry, executor=self.executor,
            event_bus=self.bus, ctx_enabled=False,
        )
        ans = s.chat("q")
        self.assertIn("终止", ans)
        errors = [e for e in self.events if isinstance(e, Error)]
        self.assertTrue(any(err.where == "session" for err in errors))

    def test_unsupported_fc_returns_text(self):
        # think 在不支持 FC 时返回 [text, None]
        llm = FakeLLM([["纯文本回答", None]])
        s = self._make_session(llm)
        ans = s.chat("q")
        self.assertEqual(ans, "纯文本回答")

    def test_unexpected_result_emits_error(self):
        llm = FakeLLM(["bad string result"])
        s = self._make_session(llm)
        ans = s.chat("q")
        # 用户输入已先进入 canonical history，最终失败追加明确 turn_failed。
        self.assertIn("请求无效", ans)
        errors = [e for e in self.events if isinstance(e, Error)]
        self.assertTrue(any(err.where == "llm" for err in errors))
        self.assertEqual((s.history[-1].metadata or {}).get("kind"), "turn_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
