"""LLM provider 错误分类与 Session 失败事务测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from agent.event_bus import EventBus
from agent.events import Done, Error
from agent.llm_errors import (
    LLMAuthenticationError,
    LLMContextOverflowError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMRequestError,
    LLMTransportError,
    classify_llm_exception,
)
from agent.session import AgentSession
from agent.work_context import LocalSessionStore
from core.message import Message


class _FakeStatusError(Exception):
    def __init__(self, message: str, *, status_code: int, code: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.body = {"error": {"code": code, "message": message}} if code else {"error": {"message": message}}
        self.request_id = "req_test"


class TestClassifyLLMException(unittest.TestCase):
    def test_status_mapping(self):
        self.assertIsInstance(
            classify_llm_exception(_FakeStatusError("bad key", status_code=401)),
            LLMAuthenticationError,
        )
        self.assertIsInstance(
            classify_llm_exception(_FakeStatusError("slow down", status_code=429)),
            LLMRateLimitError,
        )
        self.assertIsInstance(
            classify_llm_exception(_FakeStatusError("boom", status_code=503)),
            LLMTransportError,
        )

    def test_explicit_overflow_only(self):
        overflow = classify_llm_exception(
            _FakeStatusError(
                "This model's maximum context length is 128000 tokens",
                status_code=400,
                code="context_length_exceeded",
            )
        )
        self.assertIsInstance(overflow, LLMContextOverflowError)

    def test_max_tokens_param_error_is_not_overflow(self):
        err = classify_llm_exception(
            _FakeStatusError(
                "Unsupported parameter: 'max_tokens' is not supported with this model",
                status_code=400,
            )
        )
        self.assertIsInstance(err, LLMInvalidRequestError)
        self.assertNotIsInstance(err, LLMContextOverflowError)

    def test_unknown_400_is_invalid_not_retryable(self):
        err = classify_llm_exception(_FakeStatusError("weird field", status_code=400))
        self.assertIsInstance(err, LLMInvalidRequestError)
        self.assertFalse(err.retryable)


class _FailingLLM:
    def __init__(self, exc: Exception):
        self.model = "test-model"
        self.provider = "test"
        self.current_model_key = "test/test-model"
        self.is_Function_Calling = True
        self._exc = exc
        self.calls = 0

    def think(self, *args, **kwargs):
        self.calls += 1
        raise self._exc


class _Registry:
    def get_tools_description_openai_schema(self):
        return []

    def list_tools(self):
        return []


class TestSessionRecordsFailedTurns(unittest.TestCase):
    def _session(self, store: LocalSessionStore, llm: Any) -> AgentSession:
        session = AgentSession(
            llm=llm,
            registry=_Registry(),
            executor=SimpleNamespace(execute=lambda *a, **k: []),
            event_bus=EventBus(),
            session_store=store,
            ctx_enabled=False,
            max_tool_rounds=2,
        )
        session._append_history([
            Message.create_user_message("旧问题"),
            Message.create_assistant_message("旧回答"),
        ], turn_id="old-turn")
        return session

    def test_provider_failure_keeps_user_and_appends_failure_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            llm = _FailingLLM(LLMInvalidRequestError(message="bad request", status_code=400, retryable=False))
            session = self._session(store, llm)
            history_before = [message.to_dict() for message in session.history]

            events: List[Any] = []
            session.event_bus.subscribe(lambda e: events.append(e))
            answer = session.chat("新问题")

            self.assertIn("请求无效", answer)
            self.assertEqual(
                [message.to_dict() for message in session.history[:2]],
                history_before,
            )
            self.assertTrue(any(
                message.role.value == "user" and "新问题" in str(message.content)
                for message in session.history
            ))
            self.assertEqual(
                (session.history[-1].metadata or {}).get("kind"),
                "turn_failed",
            )
            self.assertTrue(any(isinstance(e, Error) for e in events))
            self.assertTrue(any(isinstance(e, Done) for e in events))
            self.assertTrue((store.active_dir / "history.jsonl").exists())

    def test_think_none_appends_failure_boundary_instead_of_empty_assistant(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / ".cbagent" / "sessions"
            store = LocalSessionStore(root)
            llm = SimpleNamespace(
                model="test-model",
                provider="test",
                current_model_key="test/test-model",
                is_Function_Calling=True,
                think=lambda *a, **k: None,
            )
            session = self._session(store, llm)
            before = len(session.history)
            answer = session.chat("应失败")
            self.assertIn("请求", answer)
            self.assertGreater(len(session.history), before)
            self.assertEqual(
                (session.history[-1].metadata or {}).get("kind"),
                "turn_failed",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
