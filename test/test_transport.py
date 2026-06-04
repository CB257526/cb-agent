"""Stage 5 transport 单测：JSON-RPC + Gateway。

四组：
- TestJsonRpcSerialize：事件序列化、response/error 构造
- TestStdioTransport：write 加锁、read_loop 解析、parse error 回写、EOF 退出
- TestGatewayDispatch：用 FakeLLM 跑端到端，断言事件流符合 JSON-RPC notification 格式
- TestGatewayRPC：cancel / quit / clear_history / busy / 未知 method
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

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

from agent.event_bus import EventBus
from agent.events import Cancelled, Done, TextDelta, ToolComplete, ToolStart
from agent.executor import ToolExecutor
from agent.session import AgentSession
from agent.transport import Gateway, StdioTransport, make_event_message, make_response
from agent.work_context import LocalSessionStore
from skills.skill_manager import SkillManager


# ========== JSON-RPC 序列化 ==========


class TestJsonRpcSerialize(unittest.TestCase):

    def test_event_to_jsonrpc_notification(self):
        ev = TextDelta(delta="hi", accumulated="hi", round_idx=1)
        msg = make_event_message(ev)
        self.assertEqual(msg["jsonrpc"], "2.0")
        self.assertEqual(msg["method"], "event")
        self.assertNotIn("id", msg)  # notification 无 id
        self.assertEqual(msg["params"]["type"], "text_delta")
        self.assertEqual(msg["params"]["delta"], "hi")
        self.assertEqual(msg["params"]["round_idx"], 1)
        # 全部字段必须 JSON 可序列化
        json.dumps(msg)

    def test_tool_start_serializes_arguments(self):
        ev = ToolStart(call_id="c1", name="search", arguments={"q": "x", "k": 3}, round_idx=2)
        msg = make_event_message(ev)
        self.assertEqual(msg["params"]["arguments"], {"q": "x", "k": 3})

    def test_response_success(self):
        m = make_response("abc", result={"status": "ok"})
        self.assertEqual(m, {"jsonrpc": "2.0", "id": "abc", "result": {"status": "ok"}})

    def test_response_error(self):
        m = make_response("abc", error={"code": -32601, "message": "no method"})
        self.assertNotIn("result", m)
        self.assertEqual(m["error"]["code"], -32601)


# ========== StdioTransport ==========


class TestStdioTransport(unittest.TestCase):

    def test_write_appends_newline_and_flushes(self):
        out = io.StringIO()
        t = StdioTransport(stdin=io.StringIO(""), stdout=out)
        ok = t.write({"jsonrpc": "2.0", "method": "event", "params": {"a": 1}})
        self.assertTrue(ok)
        line = out.getvalue()
        self.assertTrue(line.endswith("\n"))
        msg = json.loads(line)
        self.assertEqual(msg["params"]["a"], 1)

    def test_read_loop_yields_one_per_line(self):
        stdin = io.StringIO(
            '{"jsonrpc":"2.0","id":"1","method":"a"}\n'
            '\n'  # 空行跳过
            '{"jsonrpc":"2.0","id":"2","method":"b"}\n'
        )
        t = StdioTransport(stdin=stdin, stdout=io.StringIO())
        msgs = list(t.read_loop())
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["method"], "a")
        self.assertEqual(msgs[1]["method"], "b")

    def test_read_loop_writes_parse_error_on_invalid_json(self):
        stdin = io.StringIO("not json\n" + '{"jsonrpc":"2.0","id":"x","method":"ok"}\n')
        out = io.StringIO()
        t = StdioTransport(stdin=stdin, stdout=out)
        msgs = list(t.read_loop())
        # 非法行被吞，但 parse error 写到 stdout
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["method"], "ok")
        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["error"]["code"], -32700)

    def test_read_loop_writes_invalid_request_on_non_object(self):
        stdin = io.StringIO('"not an object"\n')
        out = io.StringIO()
        t = StdioTransport(stdin=stdin, stdout=out)
        msgs = list(t.read_loop())
        self.assertEqual(msgs, [])
        responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        self.assertEqual(responses[0]["error"]["code"], -32600)

    def test_write_concurrent_no_interleave(self):
        """两个线程并发 write，不会出现一行 JSON 被另一行截断。"""
        out = io.StringIO()
        t = StdioTransport(stdin=io.StringIO(""), stdout=out)

        def writer(prefix: str):
            for i in range(50):
                t.write({"jsonrpc": "2.0", "method": "event",
                         "params": {"who": prefix, "i": i, "filler": "x" * 100}})

        threads = [threading.Thread(target=writer, args=(p,)) for p in ("A", "B")]
        for th in threads: th.start()
        for th in threads: th.join()

        for line in out.getvalue().splitlines():
            if not line.strip(): continue
            msg = json.loads(line)  # 每一行都必须是合法 JSON
            self.assertIn(msg["params"]["who"], ("A", "B"))


# ========== Gateway 端到端 ==========


class FakeLLM:
    is_Function_Calling = True
    model = "fake"

    def __init__(self, outputs):
        self.outputs = list(outputs)

    def think(self, messages, tools=None, event_bus=None,
              cancel_event=None, round_idx=0):
        if not self.outputs:
            return {"answer": "", "tool_calls": []}
        out = self.outputs.pop(0)
        # 模拟流式 emit 一个 TextDelta
        if event_bus and out.get("answer"):
            event_bus.emit(TextDelta(
                delta=out["answer"], accumulated=out["answer"], round_idx=round_idx,
            ))
        return out


def _make_session_for_gateway(
    llm: FakeLLM,
    session_store=None,
    mcp_status_provider=None,
    mcp_background_loader=None,
    skill_manager=None,
):
    bus = EventBus()
    reg = MagicMock()
    reg.execute_tool = MagicMock(return_value="{}")
    reg.get_tools_description_openai_schema = MagicMock(return_value=[])
    reg.get_tools_description = MagicMock(return_value="")

    ex = ToolExecutor(reg.execute_tool, bus)
    s = AgentSession(
        llm=llm, registry=reg, executor=ex, event_bus=bus,
        memory_loader=None, skill_manager=skill_manager, ctx_enabled=False,
        session_store=session_store,
    )
    if mcp_status_provider is not None:
        s.mcp_status_provider = mcp_status_provider
    if mcp_background_loader is not None:
        s.mcp_background_loader = mcp_background_loader
    return s, bus


class _PipeStdin:
    """阻塞的 stdin 模拟：可以从外部 push line，read_loop 会一行行读到，close 后 EOF。"""

    def __init__(self):
        self._lines: List[str] = []
        self._cv = threading.Condition()
        self._closed = False

    def push(self, line: str):
        with self._cv:
            self._lines.append(line)
            self._cv.notify_all()

    def close(self):
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    def __iter__(self):
        return self

    def __next__(self):
        with self._cv:
            while not self._lines and not self._closed:
                self._cv.wait()
            if self._lines:
                return self._lines.pop(0)
            raise StopIteration


class TestGatewayDispatch(unittest.TestCase):

    def _run_gateway_with_msgs(self, llm: FakeLLM, msgs: List[str], wait_for: int = 0,
                                wait_done: bool = False, session_store=None,
                                mcp_status_provider=None,
                                mcp_background_loader=None,
                                skill_manager=None) -> List[Dict[str, Any]]:
        """起 gateway，把 msgs 一行行喂进 stdin，等收到至少 wait_for 条 stdout 行
        （或看到 done 事件，wait_done=True 时），然后关 stdin、join 主线程。
        返回 stdout 解析出的所有 JSON 消息（顺序）。
        """
        session, bus = _make_session_for_gateway(
            llm,
            session_store=session_store,
            mcp_status_provider=mcp_status_provider,
            mcp_background_loader=mcp_background_loader,
            skill_manager=skill_manager,
        )
        stdin = _PipeStdin()
        out = io.StringIO()
        out_lock = threading.Lock()

        # 包一层 stdout，让我们能在主线程阻塞等条件
        cv = threading.Condition()
        parsed: List[Dict[str, Any]] = []

        class Capturing:
            def write(self_, s):
                with out_lock:
                    out.write(s)
                # 每次写入尝试解析最新行
                with cv:
                    try:
                        for line in out.getvalue().splitlines():
                            pass
                        # 重新解析全部
                        parsed.clear()
                        for line in out.getvalue().splitlines():
                            line = line.strip()
                            if line:
                                parsed.append(json.loads(line))
                    except Exception:
                        pass
                    cv.notify_all()
                return len(s)

            def flush(self_):
                pass

        cap = Capturing()
        transport = StdioTransport(stdin=stdin, stdout=cap)  # type: ignore[arg-type]

        gw = Gateway(
            session=session, event_bus=bus, transport=transport,
            redirect_stdout_to_stderr=False,
        )
        t = threading.Thread(target=gw.serve_forever, daemon=True)
        t.start()

        # 等 ready 事件出来再开始投递（防止 stdin 在 loop 起来前就 EOF）
        deadline = time.time() + 2.0
        with cv:
            while time.time() < deadline:
                if any(m.get("params", {}).get("type") == "gateway_ready" for m in parsed):
                    break
                cv.wait(timeout=0.05)

        for m in msgs:
            stdin.push(m + "\n")

        # 等条件
        deadline = time.time() + 3.0
        with cv:
            while time.time() < deadline:
                if wait_done and any(
                    m.get("params", {}).get("type") == "done" for m in parsed
                ):
                    break
                if not wait_done and len(parsed) >= wait_for:
                    break
                cv.wait(timeout=0.05)

        stdin.close()
        t.join(timeout=2.0)
        return parsed

    def test_gateway_emits_ready_then_event_stream(self):
        llm = FakeLLM([{"answer": "你好", "tool_calls": []}])
        msgs = self._run_gateway_with_msgs(
            llm,
            [json.dumps({"jsonrpc": "2.0", "id": "p1",
                         "method": "prompt.submit",
                         "params": {"text": "hi"}})],
            wait_done=True,
        )

        # 至少有：ready / accept 响应 / text_delta / done
        types = [m.get("params", {}).get("type") for m in msgs if m.get("method") == "event"]
        self.assertIn("gateway_ready", types)
        self.assertIn("text_delta", types)
        self.assertIn("done", types)
        ready = [m for m in msgs if m.get("params", {}).get("type") == "gateway_ready"][0]
        self.assertIn("context_window", ready["params"])
        self.assertEqual(ready["params"]["context_window"]["scope"], "state+history")
        done = [m for m in msgs if m.get("params", {}).get("type") == "done"][0]
        self.assertIn("context_window", done["params"])
        self.assertGreater(done["params"]["context_window"]["used_tokens"], 0)
        # accept 响应：id=p1, result.status=accepted
        accepts = [m for m in msgs if m.get("id") == "p1"]
        self.assertEqual(len(accepts), 1)
        self.assertEqual(accepts[0]["result"]["status"], "accepted")

    def test_gateway_unknown_method(self):
        llm = FakeLLM([])
        msgs = self._run_gateway_with_msgs(
            llm,
            [json.dumps({"jsonrpc": "2.0", "id": "u1", "method": "no.such"})],
            wait_for=2,  # ready + error response
        )
        errs = [m for m in msgs if m.get("id") == "u1"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["error"]["code"], -32601)

    def test_gateway_invalid_params(self):
        llm = FakeLLM([])
        msgs = self._run_gateway_with_msgs(
            llm,
            [json.dumps({"jsonrpc": "2.0", "id": "u1",
                         "method": "prompt.submit",
                         "params": {"text": ""}})],
            wait_for=2,
        )
        errs = [m for m in msgs if m.get("id") == "u1"]
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["error"]["code"], -32602)

    def test_gateway_quit_response_and_exit(self):
        llm = FakeLLM([])
        msgs = self._run_gateway_with_msgs(
            llm,
            [json.dumps({"jsonrpc": "2.0", "id": "q1", "method": "session.quit"})],
            wait_for=2,
        )
        replies = [m for m in msgs if m.get("id") == "q1"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["result"]["bye"], True)

    def test_gateway_clear_history(self):
        llm = FakeLLM([])
        msgs = self._run_gateway_with_msgs(
            llm,
            [json.dumps({"jsonrpc": "2.0", "id": "c1", "method": "session.clear_history"})],
            wait_for=2,
        )
        replies = [m for m in msgs if m.get("id") == "c1"]
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["result"]["cleared"], True)

    def test_gateway_compact_context(self):
        """session.compact 返回压缩 payload，并写入当前 session 的 compact 快照。"""
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            store.append_turn(
                user_query="旧问题一",
                final_answer="旧回答一",
                work_record=None,
            )
            store.append_turn(
                user_query="旧问题二",
                final_answer="旧回答二",
                work_record=None,
            )

            msgs = self._run_gateway_with_msgs(
                FakeLLM([]),
                [json.dumps({"jsonrpc": "2.0", "id": "cp1", "method": "session.compact"})],
                wait_for=2,
                session_store=store,
            )

            replies = [m for m in msgs if m.get("id") == "cp1"]
            self.assertEqual(len(replies), 1)
            result = replies[0]["result"]
            self.assertIn("【上下文压缩】", result["summary"])
            self.assertGreater(result["before_messages"], result["after_messages"])
            self.assertIn("context_window", result)
            self.assertGreater(result["context_window"]["used_tokens"], 0)
            self.assertTrue(result["persisted"])
            self.assertTrue((store.active_dir / "compact.json").exists())

    def test_gateway_list_tools(self):
        """session.list_tools 应返回 registry 里的工具名/描述/schema 列表。"""
        llm = FakeLLM([])
        msgs = self._run_gateway_with_msgs(
            llm,
            [json.dumps({"jsonrpc": "2.0", "id": "lt1", "method": "session.list_tools"})],
            wait_for=2,
        )
        replies = [m for m in msgs if m.get("id") == "lt1"]
        self.assertEqual(len(replies), 1)
        result = replies[0]["result"]
        self.assertIn("tools", result)
        self.assertIsInstance(result["tools"], list)
        # 真 registry 至少注册了若干工具，断言形状即可
        if result["tools"]:
            t = result["tools"][0]
            self.assertIn("name", t)
            self.assertIn("description", t)
            # schema 可为 None（无参函数工具时）但 key 应存在
            self.assertIn("schema", t)

    def test_gateway_load_skill(self):
        """session.load_skill 返回用户显式加载的 Skill 内容。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "manual-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: manual-skill\ndescription: manual skill\n---\nmanual body $ARGUMENTS\n",
                encoding="utf-8",
            )
            manager = SkillManager(skills_dir=root)

            msgs = self._run_gateway_with_msgs(
                FakeLLM([]),
                [json.dumps({
                    "jsonrpc": "2.0",
                    "id": "sk1",
                    "method": "session.load_skill",
                    "params": {"name": "manual-skill", "args": "hello"},
                })],
                wait_for=2,
                skill_manager=manager,
            )

            replies = [m for m in msgs if m.get("id") == "sk1"]
            self.assertEqual(len(replies), 1)
            self.assertEqual(replies[0]["result"]["name"], "manual-skill")
            self.assertIn("manual body hello", replies[0]["result"]["content"])

    def test_gateway_load_skill_without_name_lists_skills(self):
        """session.load_skill 不传 name 时返回 Skill 列表。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill_dir = root / "manual-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: manual-skill\ndescription: manual skill\n---\nmanual body\n",
                encoding="utf-8",
            )
            manager = SkillManager(skills_dir=root)

            msgs = self._run_gateway_with_msgs(
                FakeLLM([]),
                [json.dumps({
                    "jsonrpc": "2.0",
                    "id": "sk-list",
                    "method": "session.load_skill",
                    "params": {"name": ""},
                })],
                wait_for=2,
                skill_manager=manager,
            )

            replies = [m for m in msgs if m.get("id") == "sk-list"]
            self.assertEqual(len(replies), 1)
            self.assertIsNone(replies[0]["result"]["name"])
            self.assertIn("已发现 1 个 Skill", replies[0]["result"]["content"])
            self.assertIn("manual-skill", replies[0]["result"]["content"])

    def test_gateway_mcp_status_rpc_and_ready_starts_background_loader(self):
        """gateway_ready 后触发 MCP 后台加载，session.mcp_status 返回当前快照。"""
        calls: List[str] = []

        def starter():
            calls.append("start")
            return {
                "status": "loading",
                "servers": [{"name": "filesystem", "status": "connecting", "tools_count": 0}],
                "total": 1,
                "connected": 0,
                "failed": 0,
            }

        msgs = self._run_gateway_with_msgs(
            FakeLLM([]),
            [json.dumps({"jsonrpc": "2.0", "id": "mcp1", "method": "session.mcp_status"})],
            wait_for=2,
            mcp_background_loader=starter,
        )

        self.assertGreaterEqual(len(calls), 1)
        replies = [m for m in msgs if m.get("id") == "mcp1"]
        self.assertEqual(len(replies), 1)
        result = replies[0]["result"]
        self.assertEqual(result["status"], "loading")
        self.assertEqual(result["servers"][0]["name"], "filesystem")

    def test_gateway_session_create_list_and_switch(self):
        """session.* 会话 RPC 返回摘要和普通 history，且能切回旧会话。"""
        with tempfile.TemporaryDirectory() as td:
            store = LocalSessionStore(Path(td) / ".cbagent" / "sessions")
            first_id = store.active_session_id
            store.append_turn(
                user_query="第一会话问题",
                final_answer="第一轮回答",
                work_record=None,
            )
            second = store.create_session()
            second_id = second["session_id"]
            store.append_turn(
                user_query="第二会话问题",
                final_answer="第二轮回答",
                work_record=None,
            )

            msgs = self._run_gateway_with_msgs(
                FakeLLM([]),
                [
                    json.dumps({"jsonrpc": "2.0", "id": "ls1", "method": "session.list_sessions"}),
                    json.dumps({"jsonrpc": "2.0", "id": "new1", "method": "session.create"}),
                    json.dumps({
                        "jsonrpc": "2.0",
                        "id": "sw1",
                        "method": "session.switch",
                        "params": {"session_id": first_id},
                    }),
                ],
                wait_for=4,
                session_store=store,
            )

            list_reply = [m for m in msgs if m.get("id") == "ls1"][0]
            sessions = list_reply["result"]["sessions"]
            self.assertGreaterEqual(len(sessions), 2)
            self.assertEqual({first_id, second_id}.issubset({s["session_id"] for s in sessions}), True)

            create_reply = [m for m in msgs if m.get("id") == "new1"][0]
            self.assertEqual(create_reply["result"]["history"], [])
            self.assertNotEqual(create_reply["result"]["session"]["session_id"], second_id)

            switch_reply = [m for m in msgs if m.get("id") == "sw1"][0]
            history_text = "\n".join(item["content"] for item in switch_reply["result"]["history"])
            self.assertIn("第一会话问题", history_text)
            self.assertIn("第一轮回答", history_text)
            self.assertNotIn("第二会话问题", history_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
