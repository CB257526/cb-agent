"""QQTool 文件与媒体参数处理。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Tuple

from agent.qq.action_bridge import global_qq_action_bridge
from agent.qq.file_delivery import QQFileDeliveryManager
from agent.qq.config import QQConfig


def prepare_file_reference(path: str, *, timeout: float | None = None) -> Tuple[str, Dict[str, Any]]:
    """把本地路径转换成 NapCat 可读取的引用。

    首选走 adapter 暴露的 ``prepare_resource_reference``，这样会复用运行中 QQ transport 的
    文件交付配置与临时 HTTP 服务。单测或离线场景没有 adapter 时，退回到
    ``QQFileDeliveryManager`` 直接生成计划。
    """

    if global_qq_action_bridge.is_ready():
        result = global_qq_action_bridge.call(
            "__cbagent_prepare_resource_reference__",
            {"path": path},
            timeout=timeout,
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else result
        ref = str(data.get("ref") or "")
        if ref:
            meta = dict(data.get("metadata") or {})
            return ref, meta

    manager = QQFileDeliveryManager(QQConfig.from_env())
    plan = manager.build_plan(path)
    if not plan.candidates:
        detail = "；".join(item for item in plan.errors if item) or "没有可用的文件交付方式"
        raise RuntimeError(detail)
    candidate = plan.candidates[0]
    return candidate.ref, {
        "delivery_method": candidate.method,
        "delivery_note": candidate.note,
        "source_path": candidate.source_path,
        "size": candidate.size,
        "errors": plan.errors,
    }


async def prepare_file_reference_async(path: str, *, config: QQConfig) -> Tuple[str, Dict[str, Any]]:
    """给 adapter 内部特殊 action 使用的异步文件交付入口。"""

    manager = QQFileDeliveryManager(config)
    plan = await asyncio.to_thread(manager.build_plan, path)
    if not plan.candidates:
        detail = "；".join(item for item in plan.errors if item) or "没有可用的文件交付方式"
        raise RuntimeError(detail)
    candidate = plan.candidates[0]
    return candidate.ref, {
        "delivery_method": candidate.method,
        "delivery_note": candidate.note,
        "source_path": candidate.source_path,
        "size": candidate.size,
        "errors": plan.errors,
    }


def normalize_path(value: Any) -> str:
    """保留 URL/base64 等外部引用，普通路径则转为本机绝对路径。"""

    text = str(value or "").strip().strip('"').strip("'")
    lower = text.lower()
    if lower.startswith(("http://", "https://", "base64://", "data:", "file://")):
        return text
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return str(path.resolve(strict=False))


__all__ = ["normalize_path", "prepare_file_reference", "prepare_file_reference_async"]
