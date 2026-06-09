"""QQTool 文件与媒体参数处理。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, Tuple

from agent.qq.action_bridge import global_qq_action_bridge
from agent.qq.file_delivery import QQFileDeliveryManager, delivery_metadata, is_external_file_reference
from agent.qq.config import QQConfig


def prepare_file_reference(path: str, *, timeout: float | None = None) -> Tuple[str, Dict[str, Any]]:
    """把本地路径转换成 NapCat 可读取的引用。

    首选走 adapter 暴露的 ``prepare_resource_reference``，这样会复用运行中 QQ transport 的
    文件交付配置与临时 HTTP 服务。单测或离线场景没有 adapter 时，退回到
    ``QQFileDeliveryManager`` 直接生成计划。
    """

    config = QQConfig.from_env()
    prepared = _already_prepared_reference(path, config=config)
    if prepared is not None:
        return prepared

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

    manager = QQFileDeliveryManager(config)
    plan = manager.build_plan(path)
    if not plan.candidates:
        detail = "；".join(item for item in plan.errors if item) or "没有可用的文件交付方式"
        raise RuntimeError(detail)
    candidate = plan.candidates[0]
    return candidate.ref, delivery_metadata(candidate, plan)


async def prepare_file_reference_async(path: str, *, config: QQConfig) -> Tuple[str, Dict[str, Any]]:
    """给 adapter 内部特殊 action 使用的异步文件交付入口。"""

    prepared = _already_prepared_reference(path, config=config)
    if prepared is not None:
        return prepared

    manager = QQFileDeliveryManager(config)
    plan = await asyncio.to_thread(manager.build_plan, path)
    if not plan.candidates:
        detail = "；".join(item for item in plan.errors if item) or "没有可用的文件交付方式"
        raise RuntimeError(detail)
    candidate = plan.candidates[0]
    return candidate.ref, delivery_metadata(candidate, plan)


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


def _already_prepared_reference(path: str, *, config: QQConfig) -> Tuple[str, Dict[str, Any]] | None:
    """识别已经可被 NapCat 直接读取的引用，避免把容器路径当宿主机路径二次交付。

    典型场景是模型先调用内部 ``__cbagent_prepare_resource_reference__`` 得到
    ``/app/cb-agent-outbound/xxx.png``，随后又把这个路径传给 ``send_group_msg``。
    如果再次进入文件交付层，宿主机上并不存在 ``/app/...``，就会报“文件不存在”。
    这里按 ``QQ_FILE_NAPCAT_PREFIX`` 判断它已经是容器内映射路径，直接透传给 NapCat。
    """

    text = str(path or "").strip()
    if not text:
        return None
    if is_external_file_reference(text):
        return text, {
            "delivery_method": "external",
            "delivery_note": "资源已经是 NapCat 可直接识别的外部引用",
            "source_path": text,
            "size": 0,
            "errors": [],
        }
    if _is_under_napcat_prefix(text, config.file_napcat_prefix):
        return text, {
            "delivery_method": "prepared_mapped_path",
            "delivery_note": "资源已经位于 QQ_FILE_NAPCAT_PREFIX 下，跳过二次交付",
            "source_path": text,
            "size": 0,
            "errors": [],
        }
    return None


def _is_under_napcat_prefix(path: str, prefix: str) -> bool:
    clean_prefix = str(prefix or "").strip().replace("\\", "/").rstrip("/")
    clean_path = str(path or "").strip().replace("\\", "/")
    # 前缀为 / 时会把所有 POSIX 绝对路径都误判为容器可读，风险太高，必须拒绝。
    if not clean_prefix or clean_prefix == "/":
        return False
    return clean_path == clean_prefix or clean_path.startswith(clean_prefix + "/")


__all__ = ["normalize_path", "prepare_file_reference", "prepare_file_reference_async"]
