"""QQTool 子功能执行层。"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

from agent.qq.action_bridge import global_qq_action_bridge

from .media import prepare_file_reference
from .registry import QQFunctionSpec, get_qq_function_spec, list_qq_function_specs


QQ_FUNCTIONS = list_qq_function_specs()


def run_qq_function(funname: str, args: Dict[str, Any], *, timeout: float | None = None) -> Dict[str, Any]:
    """执行一个 qqtool 子功能并返回结构化结果。"""

    started = time.perf_counter()
    spec = get_qq_function_spec(funname)
    if spec is None:
        return _result(
            ok=False,
            funname=str(funname or ""),
            action="",
            error=f"未知 QQTool 功能：{funname}",
            duration_ms=_elapsed_ms(started),
        )
    clean_args = dict(args or {})
    try:
        action, params, meta = _build_action_payload(spec, clean_args, timeout=timeout)
        response = global_qq_action_bridge.call(action, params, timeout=timeout)
        ok = _action_ok(response)
        data = response.get("data") if isinstance(response, dict) else response
        return _result(
            ok=ok,
            funname=spec.funname,
            action=action,
            params=_redact_params(params),
            data=_clip_data(data, limit=spec.result_limit),
            summary=_summarize_result(spec, data, ok=ok),
            metadata=meta,
            error="" if ok else _action_error_text(response),
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


def _build_action_payload(
    spec: QQFunctionSpec,
    args: Dict[str, Any],
    *,
    timeout: float | None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """按子功能定义构造 NapCat action 与参数。"""

    if spec.funname == "raw_action":
        action = str(args.get("action") or "").strip()
        if not action:
            raise ValueError("raw_action 缺少 action")
        params = args.get("params")
        if params is None:
            params = args.get("args")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("raw_action.params 必须是对象")
        return action, dict(params), {}

    missing = [key for key in spec.required if _is_missing(args.get(key))]
    if missing:
        raise ValueError(f"{spec.funname} 缺少必填参数：{', '.join(missing)}")

    params = dict(args)
    meta: Dict[str, Any] = {}
    if spec.file_param:
        raw_file = params.get(spec.file_param)
        ref, delivery_meta = prepare_file_reference(str(raw_file or ""), timeout=timeout)
        params[spec.file_param] = ref
        if "name" not in params and "file_name" in params:
            params["name"] = params.get("file_name")
        meta["file_delivery"] = delivery_meta

    if spec.funname in {"send_private_msg", "send_group_msg"}:
        params["message"] = _normalize_message(params.get("message"))
    return spec.action, params, meta


def _normalize_message(message: Any) -> Any:
    """兼容纯文本、OneBot 消息段数组和简单对象。"""

    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return message
    if isinstance(message, dict):
        return [message]
    return str(message or "")


def _action_ok(result: Dict[str, Any]) -> bool:
    status = str(result.get("status") or "").lower()
    retcode = result.get("retcode")
    if status:
        return status in {"ok", "async"}
    if retcode is not None:
        return retcode in {0, "0"}
    return True


def _action_error_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    return str(result.get("wording") or result.get("message") or result.get("error") or result)


def _clip_data(data: Any, *, limit: int) -> Any:
    """压缩返回数据，避免好友/群成员/历史消息一次性塞爆上下文。"""

    if isinstance(data, list):
        clipped = [_clip_data(item, limit=limit) for item in data[:limit]]
        payload: Dict[str, Any] = {"items": clipped, "total": len(data)}
        if len(data) > limit:
            payload["truncated"] = True
            payload["limit"] = limit
        return payload
    if isinstance(data, dict):
        result: Dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, list):
                result[key] = _clip_data(value, limit=limit)
            elif isinstance(value, dict):
                result[key] = _clip_data(value, limit=limit)
            elif isinstance(value, str) and len(value) > 1200:
                result[key] = value[:1200] + "...(truncated)"
            else:
                result[key] = value
        return result
    if isinstance(data, str) and len(data) > 1200:
        return data[:1200] + "...(truncated)"
    return data


def _summarize_result(spec: QQFunctionSpec, data: Any, *, ok: bool) -> str:
    if not ok:
        return f"{spec.funname} 调用失败"
    if isinstance(data, list):
        return f"{spec.description}成功，返回 {len(data)} 条记录"
    if isinstance(data, dict):
        for key in ("items", "message", "message_id", "nickname", "user_id", "group_id"):
            if key in data:
                return f"{spec.description}成功，关键字段 {key}={data.get(key)}"
        return f"{spec.description}成功，返回 {len(data)} 个字段"
    if data in (None, ""):
        return f"{spec.description}成功"
    return f"{spec.description}成功：{data}"


def _redact_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """日志/工具结果里折叠 base64，避免把大文件内容写入上下文。"""

    result: Dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, str) and value.startswith("base64://"):
            result[key] = f"base64://...({len(value)} chars)"
        else:
            result[key] = value
    return result


def _is_missing(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


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


__all__ = ["QQ_FUNCTIONS", "dumps_result", "run_qq_function"]
