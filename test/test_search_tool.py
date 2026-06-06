from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.tools.search import SearchTool


class FakeResponse:
    def __init__(self, status_code: int, payload: Dict[str, Any], text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Dict[str, Any]:
        return self._payload


class FakeSession:
    def __init__(self, responses: List[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if not self.responses:
            raise AssertionError("unexpected request")
        return self.responses.pop(0)


class TestSearchTool(unittest.TestCase):
    def test_no_provider_configured(self) -> None:
        with patch.dict(os.environ, {"TAVILY_API_KEY": "", "SERPAPI_API_KEY": ""}, clear=False):
            result = SearchTool(http_session=FakeSession([])).run({"query": "cb-agent"})

        self.assertIn("No web search provider is configured", result)
        self.assertIn("TAVILY_API_KEY", result)

    def test_tavily_result_includes_url_and_snippet(self) -> None:
        session = FakeSession([
            FakeResponse(
                200,
                {
                    "answer": "Short answer",
                    "results": [
                        {
                            "title": "Example result",
                            "url": "https://example.com/article",
                            "content": "A concise search snippet.",
                        }
                    ],
                },
            )
        ])

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly", "SERPAPI_API_KEY": ""}, clear=False):
            result = SearchTool(http_session=session).run({"query": "latest example", "max_results": 1})

        self.assertIn("Web search results via Tavily", result)
        self.assertIn("Answer: Short answer", result)
        self.assertIn("Example result", result)
        self.assertIn("URL: https://example.com/article", result)
        self.assertIn("Snippet: A concise search snippet.", result)
        self.assertEqual(session.calls[0]["method"], "POST")

    def test_auto_falls_back_to_serpapi(self) -> None:
        session = FakeSession([
            FakeResponse(503, {}, text="temporarily unavailable"),
            FakeResponse(
                200,
                {
                    "answer_box": {"answer": "Direct SerpApi answer"},
                    "organic_results": [
                        {
                            "title": "Fallback result",
                            "link": "https://example.org/fallback",
                            "snippet": "Fallback snippet.",
                        }
                    ],
                },
            ),
        ])

        with patch.dict(os.environ, {"TAVILY_API_KEY": "tvly", "SERPAPI_API_KEY": "serp"}, clear=False):
            result = SearchTool(http_session=session).run({"query": "fallback query", "source": "auto"})

        self.assertIn("Web search results via SerpApi", result)
        self.assertIn("Direct SerpApi answer", result)
        self.assertIn("URL: https://example.org/fallback", result)
        self.assertEqual([call["method"] for call in session.calls], ["POST", "GET"])

    def test_invalid_max_results(self) -> None:
        result = SearchTool(http_session=FakeSession([])).run({"query": "x", "max_results": 99})

        self.assertIn("max_results must be between", result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
