"""load_image 的 ImageRef 桥接与真实 tool_call_id 测试。"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.event_bus import EventBus
from agent.executor import ToolExecutor
from agent.media_store import (
    MediaBlobStore,
    reset_current_media_store,
    set_current_media_store,
)
from agent.session import AgentSession
from agent.tool_execution import ToolModelResult
from agent.work_context import LocalSessionStore
from tools.tools.load_image_tool import LoadImageTool
from tools.toolRegistry import ToolRegistry

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f\x00\x05"
    b"\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_tool_model_result_rejects_raw_image_url() -> None:
    """工具不能绕过 MediaBlobStore 把 data URI 直接塞进 checkpoint/history。"""

    with pytest.raises(ValueError, match="只支持 image_ref"):
        ToolModelResult(
            text="bad",
            content=({
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,secret"},
            },),
        )


def test_load_image_returns_image_ref_model_content_without_base64() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        image = root / "screen.png"
        image.write_bytes(PNG_BYTES)
        store = MediaBlobStore(root / ".cbagent" / "media")
        media_token = set_current_media_store(store)
        try:
            with patch(
                "constant.llm.constant_llm.ConstantLLM.resolve_image_ability",
                return_value=True,
            ):
                result = LoadImageTool().run({"path": str(image)})
        finally:
            reset_current_media_store(media_token)

        assert isinstance(result, ToolModelResult)
        payload = json.loads(result.text)
        assert payload["status"] == "ok"
        assert payload["routed_as"] == "image_ref"
        assert result.content[0]["type"] == "image_ref"
        assert "base64" not in json.dumps(result.content, ensure_ascii=False)


def test_load_image_is_replayable_across_tool_loop_user_turn_and_restart() -> None:
    """桥接图片必须进入正式 history，并保持后续请求的精确结构化前缀。"""

    class RecordingLLM:
        def __init__(self, results):
            self.results = list(results)
            self.calls = []
            self.is_Function_Calling = True
            self.model = "fake"

        def think(self, messages, tools=None, **_kwargs):
            # 深拷贝冻结每次请求，避免后续 list 追加掩盖前缀改写问题。
            self.calls.append(json.loads(json.dumps(messages, ensure_ascii=False)))
            return self.results.pop(0)

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        image = root / "screen.png"
        image.write_bytes(PNG_BYTES)
        session_root = root / ".cbagent" / "sessions"
        store = LocalSessionStore(session_root)
        registry = ToolRegistry()
        registry.register_tool(LoadImageTool())
        llm = RecordingLLM([
            {
                "answer": "",
                "tool_calls": [{
                    "id": "call-load-456",
                    "type": "function",
                    "function": {
                        "name": "load_image",
                        "arguments": json.dumps({"path": str(image)}),
                    },
                }],
            },
            {"answer": "已查看", "tool_calls": []},
            {"answer": "继续完成", "tool_calls": []},
        ])
        session = AgentSession(
            llm=llm,
            registry=registry,
            executor=ToolExecutor(registry.execute_tool, EventBus()),
            event_bus=EventBus(),
            ctx_enabled=False,
            session_store=store,
        )

        with patch(
            "constant.llm.constant_llm.ConstantLLM.resolve_image_ability",
            return_value=True,
        ):
            session.chat("读取图片")
            image.unlink()
            session.chat("继续")

        first_visual_request = llm.calls[1]
        next_user_request = llm.calls[2]
        assert first_visual_request == next_user_request[:len(first_visual_request)]
        assert any(
            message.metadata
            and message.metadata.get("kind") == "tool_image_bridge"
            and message.metadata.get("tool_call_ids") == ["call-load-456"]
            and isinstance(message.content, list)
            and any(part.get("type") == "image_ref" for part in message.content)
            for message in session.history
        )
        journal = (store.active_dir / "history.jsonl").read_text(encoding="utf-8")
        assert "image_ref" in journal
        assert "data:image" not in journal

        restarted_store = LocalSessionStore(session_root)
        restarted_llm = RecordingLLM([])
        restarted = AgentSession(
            llm=restarted_llm,
            registry=registry,
            executor=ToolExecutor(registry.execute_tool, EventBus()),
            event_bus=EventBus(),
            ctx_enabled=False,
            session_store=restarted_store,
        )
        restored_request = restarted._provider_request_messages()
        assert next_user_request == restored_request[:len(next_user_request)]
