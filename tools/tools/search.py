from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv

from tools.tool import Tool
from tools.toolParameter import ToolParameter

load_dotenv()


class SearchTool(Tool):
    """Web search tool with Tavily/SerpApi fallback.

    The tool intentionally calls the providers over HTTP instead of importing
    optional SDKs, so cb-agent can start cleanly with only core dependencies.
    """

    TAVILY_ENDPOINT = "https://api.tavily.com/search"
    SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
    DEFAULT_MAX_RESULTS = 5
    MAX_RESULTS_LIMIT = 10

    def __init__(
        self,
        http_session: Optional[requests.Session] = None,
        timeout: float = 15.0,
    ) -> None:
        super().__init__(
            name="my_advanced_search",
            description=(
                "Search the web using Tavily or SerpApi. Returns concise results "
                "with titles, URLs, and snippets. Supports automatic fallback."
            ),
        )
        self.http = http_session or requests.Session()
        self.timeout = timeout

    def run(self, input_dict: dict[str, Any]) -> str:
        ok, error = self._validate(input_dict)
        if not ok:
            return f"Search parameter error: {error}"

        query = str(input_dict["query"]).strip()
        source = str(input_dict.get("source") or "auto").strip().lower()
        max_results = self._coerce_max_results(input_dict.get("max_results"))

        providers, config_error = self._resolve_providers(source)
        if config_error:
            return config_error

        errors: List[str] = []
        for provider in providers:
            try:
                if provider == "tavily":
                    result = self._search_with_tavily(query, max_results)
                else:
                    result = self._search_with_serpapi(query, max_results)
            except Exception as exc:
                errors.append(f"{provider}: {exc}")
                continue

            if result:
                return result
            errors.append(f"{provider}: no results")

        detail = "\n".join(f"- {item}" for item in errors) or "- no provider attempted"
        return (
            "All configured web search providers failed.\n"
            f"Query: {query}\n"
            f"Details:\n{detail}"
        )

    def _resolve_providers(self, source: str) -> tuple[List[str], Optional[str]]:
        available = {
            "tavily": bool(os.getenv("TAVILY_API_KEY")),
            "serpapi": bool(os.getenv("SERPAPI_API_KEY")),
        }
        if source == "auto":
            providers = [name for name in ("tavily", "serpapi") if available[name]]
            if providers:
                return providers, None
            return [], (
                "No web search provider is configured. Set TAVILY_API_KEY or "
                "SERPAPI_API_KEY in .env, then restart cb-agent."
            )

        if source not in available:
            return [], "Search parameter error: source must be one of auto, tavily, serpapi"
        if not available[source]:
            env_name = "TAVILY_API_KEY" if source == "tavily" else "SERPAPI_API_KEY"
            return [], f"{source} search is not configured. Set {env_name} in .env."
        return [source], None

    def _search_with_tavily(self, query: str, max_results: int) -> str:
        payload = {
            "api_key": os.getenv("TAVILY_API_KEY"),
            "query": query,
            "search_depth": "basic",
            "include_answer": True,
            "include_raw_content": False,
            "max_results": max_results,
        }
        data = self._request_json("POST", self.TAVILY_ENDPOINT, json=payload)

        results = data.get("results") or []
        if not isinstance(results, list):
            results = []

        lines = [f"Web search results via Tavily for: {query}"]
        answer = str(data.get("answer") or "").strip()
        if answer:
            lines.extend(["", f"Answer: {answer}"])

        formatted = self._format_results(
            [
                {
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "snippet": item.get("content"),
                }
                for item in results[:max_results]
                if isinstance(item, dict)
            ]
        )
        if not formatted:
            return ""
        lines.extend(["", *formatted])
        return "\n".join(lines)

    def _search_with_serpapi(self, query: str, max_results: int) -> str:
        params = {
            "engine": "google",
            "q": query,
            "api_key": os.getenv("SERPAPI_API_KEY"),
            "num": max_results,
        }
        data = self._request_json("GET", self.SERPAPI_ENDPOINT, params=params)

        answer_box = data.get("answer_box") if isinstance(data.get("answer_box"), dict) else {}
        organic = data.get("organic_results") or []
        if not isinstance(organic, list):
            organic = []

        lines = [f"Web search results via SerpApi for: {query}"]
        direct_answer = self._extract_serpapi_answer(answer_box)
        if direct_answer:
            lines.extend(["", f"Answer: {direct_answer}"])

        formatted = self._format_results(
            [
                {
                    "title": item.get("title"),
                    "url": item.get("link"),
                    "snippet": item.get("snippet"),
                }
                for item in organic[:max_results]
                if isinstance(item, dict)
            ]
        )
        if not formatted:
            return ""
        lines.extend(["", *formatted])
        return "\n".join(lines)

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Dict[str, Any]:
        response = self.http.request(method, url, timeout=self.timeout, **kwargs)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            message = self._clip(getattr(response, "text", ""), 300)
            raise RuntimeError(f"HTTP {response.status_code}: {message}") from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("provider returned non-JSON response") from exc
        if not isinstance(data, dict):
            raise RuntimeError("provider returned unexpected JSON shape")
        return data

    def _format_results(self, results: List[Dict[str, Any]]) -> List[str]:
        lines: List[str] = []
        for index, item in enumerate(results, 1):
            title = self._clip(item.get("title"), 140) or "(untitled)"
            url = self._clip(item.get("url"), 500)
            snippet = self._clip(item.get("snippet"), 500)
            if not url and not snippet:
                continue
            lines.append(f"[{index}] {title}")
            if url:
                lines.append(f"URL: {url}")
            if snippet:
                lines.append(f"Snippet: {snippet}")
            lines.append("")
        while lines and lines[-1] == "":
            lines.pop()
        return lines

    def _extract_serpapi_answer(self, answer_box: Dict[str, Any]) -> str:
        for key in ("answer", "snippet", "definition"):
            value = answer_box.get(key)
            if isinstance(value, str) and value.strip():
                return self._clip(value, 500)
        return ""

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="Search query. Include key terms, product names, dates, or domains when useful.",
                required=True,
            ),
            ToolParameter(
                name="source",
                type="string",
                description="Search provider: auto, tavily, or serpapi.",
                required=False,
                default="auto",
            ),
            ToolParameter(
                name="max_results",
                type="integer",
                description=f"Number of results to return, 1-{self.MAX_RESULTS_LIMIT}.",
                required=False,
                default=self.DEFAULT_MAX_RESULTS,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        ok, _ = self._validate(parameters)
        return ok

    def _validate(self, parameters: Dict[str, Any]) -> tuple[bool, str]:
        query = parameters.get("query")
        if not isinstance(query, str) or not query.strip():
            return False, "query must be a non-empty string"

        source = str(parameters.get("source") or "auto").strip().lower()
        if source not in {"auto", "tavily", "serpapi"}:
            return False, "source must be one of auto, tavily, serpapi"

        try:
            self._coerce_max_results(parameters.get("max_results"))
        except ValueError as exc:
            return False, str(exc)
        return True, ""

    def _coerce_max_results(self, value: Any) -> int:
        if value is None or value == "":
            return self.DEFAULT_MAX_RESULTS
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_results must be an integer") from exc
        if number < 1 or number > self.MAX_RESULTS_LIMIT:
            raise ValueError(f"max_results must be between 1 and {self.MAX_RESULTS_LIMIT}")
        return number

    def _clip(self, value: Any, limit: int) -> str:
        text = "" if value is None else str(value)
        text = " ".join(text.split())
        if len(text) <= limit:
            return text
        return text[: max(0, limit - 1)].rstrip() + "..."
