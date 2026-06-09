"""QQTool 子功能执行层。"""

from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from typing import Any, Dict, List, Tuple
from urllib.parse import quote, unquote

from agent.platforms.context import get_current_platform_conversation
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
        response, params, public_meta = _call_action_with_delivery_retries(
            spec,
            action,
            params,
            meta,
            timeout=timeout,
        )
        ok = _action_ok(response)
        data = response.get("data") if isinstance(response, dict) else response
        return _result(
            ok=ok,
            funname=spec.funname,
            action=action,
            params=_redact_params(params),
            data=_clip_data(data, limit=spec.result_limit),
            summary=_summarize_result(spec, data, ok=ok),
            metadata=public_meta,
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

    # 先按当前 QQ 会话补齐 group_id/user_id，再做必填校验。
    # 否则模型在当前群里省略 group_id 时会先被“缺少必填参数”挡住，
    # 即使我们本来可以从 ConversationKey 安全地推导出目标会话。
    params = _apply_current_conversation_defaults(spec.funname, dict(args))
    missing = [key for key in spec.required if _is_missing(params.get(key))]
    if missing:
        raise ValueError(f"{spec.funname} 缺少必填参数：{', '.join(missing)}")

    meta: Dict[str, Any] = {}
    if spec.file_param:
        raw_file = params.get(spec.file_param)
        ref, delivery_meta = prepare_file_reference(str(raw_file or ""), timeout=timeout)
        params[spec.file_param] = ref
        if "name" not in params and "file_name" in params:
            params["name"] = params.get("file_name")
        meta["file_delivery"] = delivery_meta

    if spec.funname in {"send_private_msg", "send_group_msg"}:
        params["message"], message_meta = _normalize_message_for_send(
            params.get("message"),
            timeout=timeout,
        )
        if message_meta:
            meta["message_file_delivery"] = message_meta
    return spec.action, params, meta


def _call_action_with_delivery_retries(
    spec: QQFunctionSpec,
    action: str,
    params: Dict[str, Any],
    meta: Dict[str, Any],
    *,
    timeout: float | None,
) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    max_attempts = _delivery_attempt_count(meta)
    last_response: Dict[str, Any] = {}
    last_params = params
    failures: List[Dict[str, Any]] = []
    for attempt_index in range(max_attempts):
        attempt_params = _params_for_delivery_attempt(spec, params, meta, attempt_index)
        response = global_qq_action_bridge.call(action, attempt_params, timeout=timeout)
        last_response = response
        last_params = attempt_params
        if _action_ok(response):
            return response, attempt_params, _public_delivery_meta(meta, attempt_index, failures)
        if max_attempts > 1:
            failures.append(_delivery_attempt_failure(meta, attempt_index, response))
    return last_response, last_params, _public_delivery_meta(meta, max_attempts - 1, failures)


def _delivery_attempt_count(meta: Dict[str, Any]) -> int:
    count = 1
    for item in _iter_delivery_meta(meta):
        candidates = _delivery_candidates(item)
        if candidates:
            count = max(count, len(candidates))
    return count


def _params_for_delivery_attempt(
    spec: QQFunctionSpec,
    params: Dict[str, Any],
    meta: Dict[str, Any],
    attempt_index: int,
) -> Dict[str, Any]:
    if attempt_index <= 0:
        return params
    result = deepcopy(params)
    file_meta = meta.get("file_delivery")
    if spec.file_param and isinstance(file_meta, dict):
        ref = _delivery_candidate_ref(file_meta, attempt_index)
        if ref:
            result[spec.file_param] = ref

    message_meta = meta.get("message_file_delivery")
    if isinstance(message_meta, list) and message_meta:
        result["message"] = _message_for_delivery_attempt(
            result.get("message"),
            message_meta,
            attempt_index,
        )
    return result


def _message_for_delivery_attempt(message: Any, metas: List[Any], attempt_index: int) -> Any:
    if isinstance(message, list):
        resource_index = 0
        updated: List[Any] = []
        for seg in message:
            if not isinstance(seg, dict):
                updated.append(seg)
                continue
            item = dict(seg)
            seg_type = str(item.get("type") or "").strip().lower()
            data = item.get("data") if isinstance(item.get("data"), dict) else {}
            if seg_type in {"image", "record", "audio", "video", "file"} and data.get("file"):
                meta = metas[resource_index] if resource_index < len(metas) else None
                resource_index += 1
                ref = _delivery_candidate_ref(meta, attempt_index)
                if ref:
                    new_data = dict(data)
                    new_data["file"] = ref
                    item["data"] = new_data
            updated.append(item)
        return updated

    if isinstance(message, str):
        resource_index = 0

        def replace(match: re.Match[str]) -> str:
            nonlocal resource_index
            meta = metas[resource_index] if resource_index < len(metas) else None
            resource_index += 1
            ref = _delivery_candidate_ref(meta, attempt_index)
            if not ref:
                return match.group(0)
            return f"{match.group('prefix')}{quote(ref, safe=':/?&=#%._+-')}"

        return re.sub(
            r"(?P<prefix>\[CQ:[^\]]*?(?:^|,)file=)(?P<value>[^,\]]+)",
            replace,
            message,
        )
    return message


def _iter_delivery_meta(meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    file_meta = meta.get("file_delivery")
    if isinstance(file_meta, dict):
        items.append(file_meta)
    message_meta = meta.get("message_file_delivery")
    if isinstance(message_meta, list):
        items.extend(item for item in message_meta if isinstance(item, dict))
    return items


def _delivery_candidates(meta: Any) -> List[Dict[str, Any]]:
    if not isinstance(meta, dict):
        return []
    raw = meta.get("_delivery_candidates")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _delivery_candidate(meta: Any, attempt_index: int) -> Dict[str, Any] | None:
    candidates = _delivery_candidates(meta)
    if not candidates:
        return None
    index = min(max(0, attempt_index), len(candidates) - 1)
    return candidates[index]


def _delivery_candidate_ref(meta: Any, attempt_index: int) -> str:
    candidate = _delivery_candidate(meta, attempt_index)
    return str((candidate or {}).get("ref") or "")


def _delivery_attempt_failure(
    meta: Dict[str, Any],
    attempt_index: int,
    response: Dict[str, Any],
) -> Dict[str, Any]:
    methods: List[str] = []
    for item in _iter_delivery_meta(meta):
        candidate = _delivery_candidate(item, attempt_index)
        method = str((candidate or {}).get("method") or "")
        if method and method not in methods:
            methods.append(method)
    return {
        "attempt": attempt_index + 1,
        "methods": methods,
        "error": _action_error_text(response)[:500],
    }


def _public_delivery_meta(
    meta: Dict[str, Any],
    attempt_index: int,
    failures: List[Dict[str, Any]],
) -> Dict[str, Any]:
    public = _strip_internal_delivery_meta(meta, attempt_index)
    if failures:
        public["delivery_attempt_failures"] = failures
    return public


def _strip_internal_delivery_meta(value: Any, attempt_index: int) -> Any:
    if isinstance(value, list):
        return [_strip_internal_delivery_meta(item, attempt_index) for item in value]
    if not isinstance(value, dict):
        return value
    result: Dict[str, Any] = {}
    candidate = _delivery_candidate(value, attempt_index)
    for key, item in value.items():
        if key == "_delivery_candidates":
            continue
        result[key] = _strip_internal_delivery_meta(item, attempt_index)
    if candidate is not None:
        result["delivery_method"] = str(candidate.get("method") or result.get("delivery_method") or "")
        result["delivery_note"] = str(candidate.get("note") or result.get("delivery_note") or "")
        result["source_path"] = str(candidate.get("source_path") or result.get("source_path") or "")
        result["size"] = candidate.get("size", result.get("size", 0))
        result["candidate_count"] = len(_delivery_candidates(value))
    return result


def _apply_current_conversation_defaults(funname: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """按当前 QQ 会话补齐常见目标 ID。

    模型在当前群聊里调用 ``send_group_msg`` 时，经常能从 prompt 中知道“当前群”，
    但仍忘记把 ``group_id`` 写进 args。权限层已经只允许操作当前会话；这里再把
    当前 ConversationKey 补进 action 参数，减少模型因为缺字段反复试错。
    """

    current = get_current_platform_conversation()
    if current is None or current.platform != "qq":
        return args
    params = dict(args)
    group_fun = {
        "send_group_msg",
        "upload_group_file",
        "upload_image_to_qun_album",
        "get_group_info",
        "get_group_info_ex",
        "get_group_member_list",
        "get_group_member_info",
        "send_group_sign",
        "get_group_signed_list",
        "get_qun_album_list",
        "get_group_album_media_list",
        "get_group_at_all_remain",
        "set_group_todo",
        "complete_group_todo",
        "cancel_group_todo",
    }
    private_fun = {"send_private_msg", "upload_private_file"}
    if current.kind == "group" and (funname in group_fun or funname in {"send_poke", "send_like"}):
        params.setdefault("group_id", current.id)
    if current.kind == "private" and (funname in private_fun or funname in {"send_poke", "send_like"}):
        params.setdefault("user_id", current.id)
    return params


def _normalize_message_for_send(
    message: Any,
    *,
    timeout: float | None,
) -> Tuple[Any, List[Dict[str, Any]]]:
    """兼容纯文本、OneBot 消息段数组和简单对象，并处理本地媒体路径。

    模型常会通过 ``send_group_msg`` 发送图片段：
    ``{"type":"image","data":{"file":"/tmp/a.png"}}``。如果 NapCat 在 Docker
    中，直接把宿主机路径交给它会失败；这里复用文件交付层，把本机路径转换成
    mapped_path/http/base64/path 候选里的第一个可用引用。
    """

    if isinstance(message, str):
        return _normalize_cq_message_string(message, timeout=timeout)
    if isinstance(message, list):
        return _normalize_message_segments(message, timeout=timeout)
    if isinstance(message, dict):
        segments, meta = _normalize_message_segments([message], timeout=timeout)
        return segments, meta
    return str(message or ""), []


def _normalize_message_segments(
    segments: List[Any],
    *,
    timeout: float | None,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    normalized: List[Any] = []
    delivery_meta: List[Dict[str, Any]] = []
    for seg in segments:
        if not isinstance(seg, dict):
            normalized.append(seg)
            continue
        item = dict(seg)
        seg_type = str(item.get("type") or "").strip().lower()
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        if seg_type in {"image", "record", "audio", "video", "file"} and data.get("file"):
            new_data = dict(data)
            ref, meta = prepare_file_reference(str(new_data.get("file") or ""), timeout=timeout)
            new_data["file"] = ref
            item["data"] = new_data
            delivery_meta.append({
                "segment_type": seg_type,
                "source": str(data.get("file") or ""),
                **meta,
            })
        normalized.append(item)
    return normalized, delivery_meta


def _normalize_cq_message_string(
    message: str,
    *,
    timeout: float | None,
) -> Tuple[str, List[Dict[str, Any]]]:
    """处理 CQ 字符串中的 file= 本地路径。

    OneBot 既支持消息段数组，也支持 ``[CQ:image,file=...]`` 这种字符串写法。权限层
    已经会检查 CQ 字符串里的 file 字段；执行层也要同步把本地路径转换成 NapCat 可读
    引用，否则 Docker 部署时数组段能发、CQ 字符串却会因为容器读不到宿主机路径失败。
    """

    delivery_meta: List[Dict[str, Any]] = []

    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        raw_value = unquote(match.group("value").strip())
        if not raw_value:
            return match.group(0)
        ref, meta = prepare_file_reference(raw_value, timeout=timeout)
        delivery_meta.append({
            "segment_type": "cq",
            "source": raw_value,
            **meta,
        })
        return f"{prefix}{quote(ref, safe=':/?&=#%._+-')}"

    normalized = re.sub(
        r"(?P<prefix>\[CQ:[^\]]*?(?:^|,)file=)(?P<value>[^,\]]+)",
        replace,
        message,
    )
    return normalized, delivery_meta


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

    redacted = _redact_value(params)
    return redacted if isinstance(redacted, dict) else {}


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        if value.startswith("base64://"):
            return f"base64://...({len(value)} chars)"
        if value.startswith("data:") and ";base64," in value[:120].lower():
            return f"data:...base64...({len(value)} chars)"
    return value


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
