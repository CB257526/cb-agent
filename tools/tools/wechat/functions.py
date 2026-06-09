"""WeChatTool 子功能执行层。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from agent.platforms.context import get_current_platform_conversation
from agent.wechat.action_bridge import global_wechat_action_bridge

from .registry import WeChatFunctionSpec, get_wechat_function_spec, list_wechat_function_specs


WECHAT_FUNCTIONS = list_wechat_function_specs()


def run_wechat_function(funname: str, args: Dict[str, Any], *, timeout: float | None = None) -> Dict[str, Any]:
    """执行一个 wechattool 子功能并返回结构化结果。"""

    started = time.perf_counter()
    action_timeout = timeout if timeout is not None else _default_action_timeout()
    spec = get_wechat_function_spec(funname)
    if spec is None:
        return _result(
            ok=False,
            funname=str(funname or ""),
            action="",
            error=f"未知 WeChatTool 功能：{funname}",
            duration_ms=_elapsed_ms(started),
        )
    try:
        params = _build_action_params(spec, dict(args or {}))
        response = global_wechat_action_bridge.call(spec.action, params, timeout=action_timeout)
        ok = bool(response.get("ok", True))
        data = response.get("data") if isinstance(response, dict) else response
        return _result(
            ok=ok,
            funname=spec.funname,
            action=spec.action,
            params=_redact_params(params),
            data=_clip_data(data, limit=spec.result_limit),
            summary=_summarize_result(spec, data, ok=ok),
            error="" if ok else str(response.get("error") or response),
            duration_ms=_elapsed_ms(started),
        )
    except Exception as exc:  # noqa: BLE001 - 工具入口必须把异常转成 JSON
        return _result(
            ok=False,
            funname=spec.funname,
            action=spec.action,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=_elapsed_ms(started),
        )


def _build_action_params(spec: WeChatFunctionSpec, args: Dict[str, Any]) -> Dict[str, Any]:
    if spec.funname == "send_text" and "text" not in args and "message" in args:
        args["text"] = args.get("message")
    missing = [key for key in spec.required if _is_missing(args.get(key))]
    if missing:
        raise ValueError(f"{spec.funname} 缺少必填参数：{', '.join(missing)}")
    if spec.funname == "send_text" and _is_missing(args.get("text")):
        raise ValueError("send_text 缺少必填参数：text")

    params = dict(args)
    if spec.file_param:
        params[spec.file_param] = _normalize_path(params.get(spec.file_param))
        if spec.funname == "send_image":
            params.setdefault("kind", "image")
        elif spec.funname == "send_file":
            params.setdefault("kind", "file")

    # 微信 OC 只处理当前账号的私聊 bot。允许模型在当前会话中省略 user_id，
    # 减少 prompt 和工具调用噪声；如果未来上游真的下发群聊字段，也不在这里
    # 自动补 group_id，避免把微信误当成 QQ 那样的群聊机器人。
    current = get_current_platform_conversation()
    if current is not None and current.platform == "wechat":
        if current.kind == "private":
            params.setdefault("user_id", current.id)
    return params


def _normalize_path(raw: Any) -> str:
    text = str(raw or "").strip().strip('"').strip("'")
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path.resolve(strict=False))


def _clip_data(data: Any, *, limit: int) -> Any:
    if isinstance(data, list):
        payload: Dict[str, Any] = {"items": data[:limit], "total": len(data)}
        if len(data) > limit:
            payload["truncated"] = True
            payload["limit"] = limit
        return payload
    if isinstance(data, dict):
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str) and len(value) > 1200:
                result[key] = value[:1200] + "...(truncated)"
            else:
                result[key] = value
        return result
    if isinstance(data, str) and len(data) > 1200:
        return data[:1200] + "...(truncated)"
    return data


def _summarize_result(spec: WeChatFunctionSpec, data: Any, *, ok: bool) -> str:
    if not ok:
        return f"{spec.funname} 调用失败"
    if isinstance(data, dict):
        return f"{spec.description}成功，返回 {len(data)} 个字段"
    return f"{spec.description}成功"


def _redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in params.items():
        if "token" in key.lower():
            result[key] = "***"
        else:
            result[key] = value
    return result


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _default_action_timeout() -> float:
    """读取 wechattool 的跨线程 action 等待超时。

    微信 adapter 内部 HTTP 请求本身有 API 超时；这里再给工具线程到 adapter 事件循环
    的等待加一道边界，避免事件循环异常卡住时工具调用长期悬挂。
    """

    try:
        return max(1.0, float(os.getenv("WECHAT_ACTION_TIMEOUT_SECONDS") or 30.0))
    except Exception:
        return 30.0


def _result(**kwargs: Any) -> Dict[str, Any]:
    payload = {
        "ok": bool(kwargs.pop("ok", False)),
        "funname": kwargs.pop("funname", ""),
        "action": kwargs.pop("action", ""),
        "duration_ms": kwargs.pop("duration_ms", 0),
    }
    payload.update(kwargs)
    return payload


def dumps_result(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


__all__ = ["WECHAT_FUNCTIONS", "dumps_result", "run_wechat_function"]
