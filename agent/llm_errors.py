"""LLM provider 请求错误的统一类型与分类。

设计目标：
1. Session 能区分 overflow / 限流 / 鉴权 / 网络 / 非法请求，而不是只拿到 None；
2. 失败回合不得被误当成“空回答成功”写入 active history / transcript；
3. 错误分类优先看 SDK 类型与 HTTP status，文本启发式必须保守，避免把
   ``max_tokens`` 参数错误误判成 context overflow。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class LLMRequestError(RuntimeError):
    """所有 LLM provider 请求失败的基类。"""

    message: str
    provider: str = ""
    model_key: str = ""
    model_id: str = ""
    status_code: Optional[int] = None
    request_id: str = ""
    retryable: bool = False
    original_type: str = ""
    round_idx: int = 0
    partial_answer: str = ""
    partial_reasoning: str = ""
    # 流式中途失败时，若 tool_calls 不完整则不应被当成正式结果。
    partial_tool_calls_complete: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def __str__(self) -> str:  # pragma: no cover - 兼容 str(exc)
        return self.message


class LLMContextOverflowError(LLMRequestError):
    """输入上下文超过模型窗口。"""


class LLMRateLimitError(LLMRequestError):
    """限流 / 429。"""


class LLMAuthenticationError(LLMRequestError):
    """鉴权失败 / 401 / 403。"""


class LLMTransportError(LLMRequestError):
    """网络、超时、连接中断等传输层错误。"""


class LLMInvalidRequestError(LLMRequestError):
    """请求本身不合法，通常不可自动重试。"""


# 只有这些明确标记才允许自动 compact + 重试。
_OVERFLOW_CODE_MARKERS = (
    "context_length_exceeded",
    "prompt_too_long",
    "context_window_exceeded",
    "string_above_max_length",
    "input_tokens_exceed",
    "max_prompt_tokens",
    "prompt_tokens_too_large",
)

_OVERFLOW_TEXT_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "prompt too long",
    "too many tokens in",
    "input tokens exceed",
    "exceeds the model",
    "exceeds model context",
    "this model's maximum context length",
    "maximum prompt length",
)

# 这些 token 相关文案经常是输出参数/配额问题，禁止单独当作 overflow。
_NON_OVERFLOW_TOKEN_MARKERS = (
    "max_tokens",
    "max_completion_tokens",
    "max_output_tokens",
    "unsupported parameter",
    "unknown parameter",
    "invalid parameter",
    "output tokens",
    "completion tokens",
    "insufficient_quota",
    "quota",
    "billing",
)


def _safe_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def _extract_status_code(exc: BaseException) -> Optional[int]:
    for attr in ("status_code", "status", "http_status"):
        raw = getattr(exc, attr, None)
        try:
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            continue
    response = getattr(exc, "response", None)
    if response is not None:
        raw = getattr(response, "status_code", None)
        try:
            if raw is not None:
                return int(raw)
        except (TypeError, ValueError):
            pass
    return None


def _extract_request_id(exc: BaseException) -> str:
    for attr in ("request_id", "requestId"):
        value = getattr(exc, attr, None)
        if value:
            return str(value)
    response = getattr(exc, "response", None)
    if response is not None:
        headers = getattr(response, "headers", None) or {}
        try:
            for key in ("x-request-id", "request-id", "x-openai-request-id"):
                if key in headers:
                    return str(headers[key])
        except Exception:
            pass
    return ""


def _extract_error_code(exc: BaseException) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            for key in ("code", "type", "param"):
                value = err.get(key)
                if value:
                    return str(value)
        for key in ("code", "type"):
            value = body.get(key)
            if value:
                return str(value)
    code = getattr(exc, "code", None)
    if code:
        return str(code)
    return ""


def _is_explicit_overflow(text: str, code: str) -> bool:
    lowered = f"{code} {text}".lower()
    if any(marker in lowered for marker in _NON_OVERFLOW_TOKEN_MARKERS):
        # 若同时出现明确 overflow 短语，仍可判定；仅有 max_tokens 参数错误则否。
        if not any(marker in lowered for marker in _OVERFLOW_TEXT_MARKERS):
            if not any(marker in lowered for marker in _OVERFLOW_CODE_MARKERS):
                return False
    if any(marker in code.lower() for marker in _OVERFLOW_CODE_MARKERS):
        return True
    if any(marker in lowered for marker in _OVERFLOW_TEXT_MARKERS):
        return True
    # 非常保守的组合：明确说 input/prompt tokens 超限。
    if re.search(r"(input|prompt).{0,24}(too many|exceed|over)", lowered):
        return True
    return False


def classify_llm_exception(
    exc: BaseException,
    *,
    provider: str = "",
    model_key: str = "",
    model_id: str = "",
    round_idx: int = 0,
    partial_answer: str = "",
    partial_reasoning: str = "",
) -> LLMRequestError:
    """把任意异常分类为统一的 LLMRequestError 子类。

    规则：
    1. SDK 类型与 HTTP status 优先；
    2. overflow 必须有明确 code/文本证据；
    3. 裸 max_tokens / 400 / invalid request 不得自动当 overflow。
    """

    status = _extract_status_code(exc)
    request_id = _extract_request_id(exc)
    code = _extract_error_code(exc)
    message = _safe_text(exc) or type(exc).__name__
    original_type = type(exc).__name__
    common = dict(
        message=message,
        provider=provider or "",
        model_key=model_key or "",
        model_id=model_id or "",
        status_code=status,
        request_id=request_id,
        original_type=original_type,
        round_idx=int(round_idx or 0),
        partial_answer=partial_answer or "",
        partial_reasoning=partial_reasoning or "",
        partial_tool_calls_complete=False,
        details={"error_code": code} if code else {},
    )

    # 已是我们自己的类型：保留子类，只补齐运行时字段。
    if isinstance(exc, LLMRequestError):
        if not exc.provider:
            exc.provider = common["provider"]
        if not exc.model_id:
            exc.model_id = common["model_id"]
        if not exc.model_key:
            exc.model_key = common["model_key"]
        if not exc.round_idx:
            exc.round_idx = common["round_idx"]
        if partial_answer and not exc.partial_answer:
            exc.partial_answer = partial_answer
        if partial_reasoning and not exc.partial_reasoning:
            exc.partial_reasoning = partial_reasoning
        return exc

    name = original_type
    module = getattr(type(exc), "__module__", "") or ""

    # ---- SDK 结构化类型 ----
    if name in {"AuthenticationError", "PermissionDeniedError"} or status in {401, 403}:
        return LLMAuthenticationError(retryable=False, **common)
    if name == "RateLimitError" or status == 429:
        return LLMRateLimitError(retryable=True, **common)
    if name in {"APITimeoutError", "APIConnectionError", "TimeoutError", "ConnectionError"}:
        return LLMTransportError(retryable=True, **common)
    if name in {"InternalServerError"} or (status is not None and status >= 500):
        return LLMTransportError(retryable=True, **common)
    if name in {"BadRequestError", "UnprocessableEntityError", "NotFoundError", "ConflictError"}:
        if _is_explicit_overflow(message, code):
            return LLMContextOverflowError(retryable=False, **common)
        return LLMInvalidRequestError(retryable=False, **common)

    # openai.APIStatusError 等通用状态错误
    if "openai" in module or name in {"APIStatusError", "APIError"}:
        if status in {401, 403}:
            return LLMAuthenticationError(retryable=False, **common)
        if status == 429:
            return LLMRateLimitError(retryable=True, **common)
        if status is not None and status >= 500:
            return LLMTransportError(retryable=True, **common)
        if _is_explicit_overflow(message, code):
            return LLMContextOverflowError(retryable=False, **common)
        if status == 400:
            return LLMInvalidRequestError(retryable=False, **common)

    # ---- 文本启发式兜底（保守）----
    if _is_explicit_overflow(message, code):
        return LLMContextOverflowError(retryable=False, **common)
    if status == 429 or "rate limit" in message.lower():
        return LLMRateLimitError(retryable=True, **common)
    if status in {401, 403} or "authentication" in message.lower() or "invalid api key" in message.lower():
        return LLMAuthenticationError(retryable=False, **common)
    if status is not None and status >= 500:
        return LLMTransportError(retryable=True, **common)
    if any(token in message.lower() for token in ("timeout", "timed out", "connection", "temporarily unavailable")):
        return LLMTransportError(retryable=True, **common)
    if status == 400 or "invalid" in message.lower():
        return LLMInvalidRequestError(retryable=False, **common)

    return LLMRequestError(retryable=False, **common)


__all__ = [
    "LLMAuthenticationError",
    "LLMContextOverflowError",
    "LLMInvalidRequestError",
    "LLMRateLimitError",
    "LLMRequestError",
    "LLMTransportError",
    "classify_llm_exception",
]
