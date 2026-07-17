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
from agent.event_bus import EventBus, collect_all
from agent.events import (
    Cancelled, Done, Error, ReasoningDelta, RoundEnd, RoundStart, TextDelta,
)
from agent.executor import ToolExecutor
from agent.session import AgentSession
from agent.work_context import LocalSessionStore
from constant.llm.constant_llm import ConstantLLM
from core.message import Message


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
_CAPABILITY_ENV_KEYS = ("IS_TOOL", "IS_REASONING", "MAX_TOKENS", "IMAGE_ABILITY")


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
        self.assertEqual(dones[0].context_window["scope"], "state+history")

    def test_context_window_uses_model_config_at_eighty_percent(self):
        """Context 指标使用 constant_llm.py 里的模型窗口，并取 80% 作为安全预算。"""
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
            self.assertEqual(usage["model_max_tokens"], 1000)
            self.assertEqual(usage["max_tokens"], 800)
            self.assertEqual(usage["threshold_ratio"], 0.8)
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_image_capable_model_sends_image_but_history_keeps_summary(self):
        """支持视觉的模型当前轮收到 image_url，但跨轮 history 不保存 data URI。"""
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
                llm = FakeLLM([{"answer": "看到了", "tool_calls": []}])
                s = self._make_session(llm)

                s.chat("图里有什么", attachments=[{"path": str(image), "source": "direct"}])

            first_messages = llm.calls[0]["messages"]
            last_user = first_messages[-1]
            self.assertIsInstance(last_user["content"], list)
            image_parts = [p for p in last_user["content"] if p.get("type") == "image_url"]
            self.assertEqual(len(image_parts), 1)
            self.assertTrue(image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,"))

            history_dump = json.dumps([m.to_dict() for m in s.history], ensure_ascii=False)
            self.assertIn("图片已原生发送", history_dump)
            self.assertNotIn("data:image", history_dump)
            self.assertNotIn("base64", history_dump)

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
        self.assertTrue(any(isinstance(e, Cancelled) and e.where == "session_loop" for e in self.events))
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
            # 结果原样回灌(result_cap 不会触发，因为 < 50k)
            self.assertIn(huge_content, tool_msgs[0].get("content", ""))
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_history_window_no_longer_cuts_tool_call_pair(self):
        """history_window 不再裁剪 active history,工具调用链应完整保留。

        构造:第一轮一个 file_read 工具调用 -> 第二轮收尾。即使手动把
        history_window 调到 2,下一轮也不应按消息数截断,因此 tool 结果仍能
        在前文找到它的 assistant.tool_calls。
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
            # history_window 只是兼容字段,不再按消息数裁剪 active history。
            s.history_window = 2

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

    def test_dynamic_context_follows_compact_boundary_slice(self):
        """P1 回归:Context% 估算与请求口径一致,走 boundary 切片。

        /compact 后 boundary 之前的原始消息不再进入下一轮 prompt,Context% 也应
        只统计 boundary(含)之后的部分,而不是物理尾部 self.history[-window:]。
        """
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
            from context.compact import make_compact_boundary_message
            # 灌一段很长的早期 history(会被 compact 切掉)
            s.history.append(Message.create_user_message("早期问题 " + "A" * 3000))
            s.history.append(Message.create_assistant_message("早期回答 " + "B" * 3000))
            text_before = s._dynamic_context_text()
            self.assertIn("A" * 100, text_before)

            # 追加 boundary(模拟 /compact);boundary 之后只有一句短消息
            s.history.append(make_compact_boundary_message("摘要"))
            s.history.append(Message.create_user_message("新问题"))

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

    def test_history_window_no_longer_truncates_after_compact_boundary(self):
        """验证 active history 在 compact boundary 之后会完整发送。

        compact_boundary 之前的历史仍会被切掉,但 boundary 之后不再按
        history_window 做消息数截断。
        """
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
            from context.compact import make_compact_boundary_message

            # 构造：boundary 锚点 + 6 条尾部消息，history_window=3 也不截断
            s.history.append(make_compact_boundary_message("ANCHOR_SUMMARY"))
            for i in range(6):
                s.history.append(Message.create_user_message(f"tail-{i}"))
            s.history_window = 3

            dicts = s._sliced_history_dicts()

            # 应该返回完整 active history：boundary + 6 条 tail
            self.assertEqual(len(dicts), 7)
            self.assertIn("ANCHOR_SUMMARY", str(dicts[0].get("content")))
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
                s.plan_store.approve()

                # 模拟旧版本已经把 plan 段写进历史 context_update 的情况。
                from core.message import Message, MessageRole
                s.history.append(Message(
                    role=MessageRole.USER,
                    content=(
                        "<context-update>\n"
                        "[Plan Mode State]\n"
                        "Approved plan for implementation:\n"
                        f"{unique}\n\n"
                        "[Local SessionState]\n"
                        "keep-this-state\n"
                        "</context-update>"
                    ),
                    metadata={"kind": "context_update"},
                ))

                sliced = json.dumps(s._sliced_history_dicts(), ensure_ascii=False)
                self.assertNotIn(unique, sliced)
                self.assertIn("keep-this-state", sliced)

                s.chat("first")
                first_request = json.dumps(llm.calls[0]["messages"], ensure_ascii=False)
                self.assertEqual(first_request.count(unique), 1)

                # 本轮落进 history 的 context_update 不应包含 plan 文本。
                history_dump = json.dumps([m.to_dict() for m in s.history], ensure_ascii=False)
                # 旧格式 plan 段保留审计，新格式再追加一次具名 plan section。
                self.assertEqual(history_dump.count(unique), 2)

                s.chat("second")
                second_request = json.dumps(llm.calls[1]["messages"], ensure_ascii=False)
                self.assertEqual(second_request.count(unique), 1)
                self.assertIn(unique, s._dynamic_context_text())
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

    def test_compact_summary_preserves_plan_state_without_copying_full_plan(self):
        """compact 摘要记录计划状态/实施进度要求,计划全文继续由 PlanState 注入。"""
        from core.message import Message

        llm = FakeLLM([])
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            s = AgentSession(
                llm=llm, registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False, session_store=store,
            )
            unique = "UNIQUE_FULL_PLAN_BODY_SHOULD_NOT_BE_COPIED"
            plan = "# Approved Plan\n\n" + "\n".join(
                f"- step {i} {unique}" for i in range(20)
            )
            s.plan_store.save_pending_plan(plan)
            s.plan_store.approve()

            summary = s._rule_compact_summary(
                messages=[Message.create_user_message("已完成 step 1, 下一步 step 2")],
                state_text="",
            )

            self.assertIn("计划状态", summary)
            self.assertIn("已批准计划全文由 PlanState 每轮注入", summary)
            self.assertNotIn(unique, summary)

    def test_preflight_auto_compact_when_full_request_exceeds_budget(self):
        """验证 preflight 只在完整请求超过安全上下文预算时触发 compact。

        场景：模型窗口 20000，context_budget = 16000（80%），
        注入 16500 × "word " 让 request_tokens 超过 context_budget，然后发 chat。

        预期行为：
        - chat 正常返回 "ok"（compact 成功释放空间后继续）
        - history 中出现了 compact_boundary 消息
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

            # 注入大量文本让 request_tokens 超过 context_budget
            s.history.append(Message.create_user_message("word " * 16500))
            answer = s.chat("continue")

            self.assertEqual(answer, "ok")
            # 验证 compact_boundary 已写入 history
            self.assertTrue(any(
                (m.metadata or {}).get("kind") == "compact_boundary"
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
            self.assertIn("a.txt", store.state_text())

            # transcript 落盘的是原始 messages
            transcript = store.active_dir / "transcript.jsonl"
            raw_transcript = transcript.read_text(encoding="utf-8")
            self.assertIn("file_read", raw_transcript)

            # 第 3 轮再问,模型仍然能在 history 里看到上一轮原始 tool_calls / tool_result
            s.chat("继续分析")
            next_turn_messages = llm.calls[2]["messages"]
            self.assertTrue(any(
                m.get("role") == "tool" and long_content in str(m.get("content", ""))
                for m in next_turn_messages
            ))

    def test_loop_microcompact_only_compacts_llm_request_copy(self):
        """验证 tool loop 中的 microcompact 只压缩 LLM 请求副本，不碰原始 history。

        场景：10 轮 file_read 工具调用，每轮返回 1500 个 "word "，累计大量
        tool_result 内容。到第 11 轮（最终回答轮）时，发给 LLM 的请求会触发
        microcompact。

        核心断言（三点验证）：
        1. **LLM 请求副本被压缩**：最后一轮发给 LLM 的 messages 中，存在 role=tool
           且 content 包含 '"cleared": true' 的消息 —— 旧 tool_result 被替换为
           占位符。
        2. **原始 history 完好无损**：self.history 中 tool 消息的 content 仍然包含
           原始长文本（long_content），没有被压缩破坏。
        3. **压缩事件被记录**：Done.auto_compact.events 中存在 reason="tool_loop"
           且 compressed_tool_messages > 0 的审计条目。

        如果 microcompact 错误地修改了原始 messages 而非副本，断言 2 会失败；
        如果 microcompact 根本没触发，断言 1 和 3 会失败。
        """
        original = ConstantLLM.llm_dict.get("fake")
        try:
            ConstantLLM.llm_dict["fake"] = {
                "is_tool": True, "is_reasoning": False,
                "image_ability": False, "max_tokens": 20000,
            }
            # 10 轮工具调用 + 1 轮最终回答
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

            # 本用例只验证 tool-loop 副本压缩；关闭回合结束后的 replacement compact，
            # 避免它按设计移除原始中间工具链而干扰断言。
            with patch.object(s, "_maybe_auto_compact_history", return_value=None):
                answer = s.chat("start")

            # 最终回答正确
            self.assertEqual(answer, "done")
            # 断言 1：LLM 最终请求中存在被 microcompact 清除的 tool 消息
            final_request = llm.calls[-1]["messages"]
            cleared_tool_messages = [
                m for m in final_request
                if m.get("role") == "tool" and '"cleared": true' in str(m.get("content"))
            ]
            self.assertGreaterEqual(len(cleared_tool_messages), 1)
            # 断言 2：self.history 中 tool 消息的原始内容完整保留
            self.assertTrue(any(
                (m.role.value if hasattr(m.role, "value") else str(m.role)) == "tool"
                and long_content in str(m.content)
                for m in s.history
            ))
            # 断言 3：压缩事件被正确记录到 Done.auto_compact
            dones = [e for e in self.events if isinstance(e, Done)]
            events = (dones[-1].auto_compact or {}).get("events", [])
            self.assertTrue(any(
                item.get("reason") == "tool_loop"
                and item.get("compressed_tool_messages", 0) > 0
                for item in events
            ))
        finally:
            if original is None:
                ConstantLLM.llm_dict.pop("fake", None)
            else:
                ConstantLLM.llm_dict["fake"] = original

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
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            call = _tc("file_read", '{"path":"active.txt"}', call_id="call_active")
            store.begin_active_turn(user_query="恢复工具检查点")
            store.record_active_assistant_tool_calls(
                round_idx=1,
                assistant_message=Message.create_assistant_message(tool_calls=[call]),
            )
            store.record_active_tool_completed(
                round_idx=1,
                tool_message=Message.create_tool_message(
                    tool_call_id="call_active",
                    tool_name="file_read",
                    tool_output=json.dumps({"path": "active.txt", "content": "abc"}, ensure_ascii=False),
                ),
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
            self.assertTrue(any(item.get("interrupted") for item in exported))
            self.assertTrue(any(item.get("tool", {}).get("call_id") == "call_active" for item in exported))

            sliced = restored._sliced_history_dicts()
            roles = [m.get("role") for m in sliced]
            self.assertEqual(roles, ["user", "assistant", "tool"])
            self.assertEqual(sliced[1]["tool_calls"][0]["id"], "call_active")
            self.assertEqual(sliced[2]["tool_call_id"], "call_active")

    def test_recovered_tool_checkpoint_survives_continue_and_second_restart(self):
        """中断轮恢复后输入“继续”，再次重启仍应保留中断轮及新一轮。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            call = _tc("bash", '{"command":"python search_download.py"}', call_id="call_download")
            store.begin_active_turn(
                user_query="你刚才下载的图片全都是无效的，重新下载真实的产品图",
                turn_id="turn_interrupted",
            )
            store.record_active_assistant_tool_calls(
                round_idx=1,
                assistant_message=Message.create_assistant_message(tool_calls=[call]),
            )
            store.record_active_tool_completed(
                round_idx=1,
                tool_message=Message.create_tool_message(
                    tool_call_id="call_download",
                    tool_name="bash",
                    tool_output="已创建 search_download.py",
                ),
            )

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

            self.assertEqual(text.count("你刚才下载的图片全都是无效的"), 1)
            self.assertEqual(text.count("继续"), 2)  # 用户输入与最终回答各出现一次。
            self.assertIn("已创建 search_download.py", text)
            self.assertIn("继续处理完成", text)
            self.assertTrue(any(
                (message.metadata or {}).get("interrupted")
                for message in visible
            ))

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

            restored = LocalSessionStore(root).load_latest_history(max_messages=20)
            visible = [
                m for m in restored
                if (m.metadata or {}).get("kind") != "context_update"
            ]

            self.assertEqual(
                [m.role.value if hasattr(m.role, "value") else str(m.role) for m in visible],
                ["user", "assistant", "tool"],
            )
            self.assertEqual([tc["id"] for tc in visible[1].tool_calls], ["call_first"])
            self.assertEqual(visible[2].tool_call_id, "call_first")

    def test_final_answer_checkpoint_survives_transcript_commit_interruption(self):
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
            s._persist_turn = MagicMock(side_effect=KeyboardInterrupt())

            with self.assertRaises(KeyboardInterrupt):
                s.chat("需要可靠恢复的回答")

            restored = AgentSession(
                llm=FakeLLM([]), registry=self.registry, executor=self.executor,
                event_bus=self.bus, ctx_enabled=False,
                session_store=LocalSessionStore(root),
            )
            visible = [
                message for message in restored.history
                if (message.metadata or {}).get("kind") != "context_update"
            ]

            self.assertEqual(
                [m.role.value if hasattr(m.role, "value") else str(m.role) for m in visible],
                ["user", "assistant"],
            )
            self.assertEqual(visible[-1].content, "已经展示给用户的回答")
            self.assertEqual(visible[-1].reasoning_content, "最终思考")
            self.assertTrue((visible[-1].metadata or {}).get("interrupted"))

    def test_transcript_is_committed_before_memory_writeback(self):
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

            self.assertFalse((store.active_dir / "active_turn.jsonl").exists())
            restored = LocalSessionStore(root).load_latest_history(max_messages=20)
            restored_text = "\n".join(str(message.content) for message in restored)
            self.assertEqual(restored_text.count("提交顺序"), 1)
            self.assertEqual(restored_text.count("先提交的回答"), 1)

    def test_active_tool_error_is_exported_as_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            call = _tc("file_read", call_id="call_error")
            store.begin_active_turn(user_query="恢复失败工具")
            store.record_active_assistant_tool_calls(
                round_idx=1,
                assistant_message=Message.create_assistant_message(tool_calls=[call]),
            )
            store.record_active_tool_completed(
                round_idx=1,
                tool_message=Message.create_tool_message(
                    "call_error",
                    "file_read",
                    '{"error":"denied"}',
                    is_error=True,
                ),
                is_error=True,
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
            self.assertEqual(len(s.history), 5)
            self.assertEqual(len(s.export_history()), 4)

            with patch("agent.session.COMPACT_RETAINED_MESSAGE_TOKENS", 20):
                payload = s.compact_context()
            self.assertEqual(payload["before_messages"], 5)
            self.assertEqual(payload["after_messages"], 3)
            self.assertTrue(payload["persisted"])
            self.assertIn("【上下文压缩】", payload["summary"])
            # replacement history 的第一条是 boundary，后面是最新回合首尾消息。
            self.assertEqual(
                (s.history[0].metadata or {}).get("kind"),
                "compact_boundary",
            )
            self.assertIn("旧问题二", str(s.history[1].content))
            self.assertIn("旧回答二", str(s.history[2].content))
            self.assertTrue((store.active_dir / "compact.json").exists())
            self.assertTrue((store.active_dir / "compactions.jsonl").exists())
            self.assertTrue((store.active_dir / "transcript.jsonl").exists())

            s.chat("继续")
            next_turn_messages = llm.calls[2]["messages"]
            context_text = "\n".join(str(m.get("content", "")) for m in next_turn_messages)
            self.assertIn("【上下文压缩】", context_text)
            # boundary 之前的旧 user/assistant 不再作为独立条目出现在请求里
            raw_user_assistant = [
                m for m in next_turn_messages
                if m.get("role") in {"user", "assistant"}
            ]
            # 旧回答一只允许出现在摘要 boundary 中，不再作为独立 assistant 消息保留。
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

            def fail_save_compaction(**kwargs):
                raise OSError("disk full")

            store.save_compaction = fail_save_compaction  # type: ignore[method-assign]

            with patch("agent.session.COMPACT_RETAINED_MESSAGE_TOKENS", 20):
                with self.assertRaises(OSError):
                    s.compact_context()

            self.assertEqual(s.export_history(), before)

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

            with patch("agent.session.COMPACT_RETAINED_MESSAGE_TOKENS", 20):
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
        # user 入了，assistant 因为空字符串没入
        self.assertEqual(len(s.history), 2)
        self.assertEqual(len(s.export_history()), 1)

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
        self.assertEqual(ans, "")
        errors = [e for e in self.events if isinstance(e, Error)]
        self.assertTrue(any(err.where == "llm" for err in errors))


if __name__ == "__main__":
    unittest.main(verbosity=2)
