"""QQ/NapCat 配置读取。

配置统一来自 .env / 环境变量，便于和现有日志、MCP、Buddy 开关保持一致。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Set


@dataclass(frozen=True)
class QQConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 6199
    access_token: str = ""
    group_mode: str = "mention"
    wake_prefix: str = "/agent"
    allowed_groups: Set[str] = field(default_factory=set)
    allowed_users: Set[str] = field(default_factory=set)
    action_timeout_seconds: float = 30.0
    file_delivery_mode: str = "path"
    file_host_prefix: str = ""
    file_napcat_prefix: str = ""
    file_http_host: str = "127.0.0.1"
    file_http_port: int = 0
    file_http_public_base_url: str = ""
    file_http_ttl_seconds: int = 300
    file_base64_max_mb: float = 3.0
    group_context_messages: int = 50
    group_context_max_chars: int = 8000

    @classmethod
    def from_env(cls) -> "QQConfig":
        mode = (os.getenv("QQ_GROUP_MODE") or "mention").strip().lower()
        if mode not in {"mention", "prefix", "all"}:
            mode = "mention"
        delivery_mode = (os.getenv("QQ_FILE_DELIVERY_MODE") or "path").strip().lower()
        if delivery_mode not in {"path", "mapped_path", "http", "base64", "auto"}:
            delivery_mode = "path"
        return cls(
            enabled=_env_bool("QQ_ENABLE", True),
            host=(os.getenv("QQ_HOST") or "127.0.0.1").strip() or "127.0.0.1",
            port=_env_int("QQ_PORT", 6199),
            access_token=(os.getenv("QQ_ACCESS_TOKEN") or "").strip(),
            group_mode=mode,
            wake_prefix=(os.getenv("QQ_WAKE_PREFIX") or "/agent").strip(),
            allowed_groups=_csv_set(os.getenv("QQ_ALLOWED_GROUPS")),
            allowed_users=_csv_set(os.getenv("QQ_ALLOWED_USERS")),
            action_timeout_seconds=max(1.0, _env_float("QQ_ACTION_TIMEOUT_SECONDS", 30.0)),
            file_delivery_mode=delivery_mode,
            file_host_prefix=(os.getenv("QQ_FILE_HOST_PREFIX") or "").strip(),
            file_napcat_prefix=(os.getenv("QQ_FILE_NAPCAT_PREFIX") or "").strip(),
            file_http_host=(os.getenv("QQ_FILE_HTTP_HOST") or "127.0.0.1").strip() or "127.0.0.1",
            file_http_port=max(0, _env_int("QQ_FILE_HTTP_PORT", 0)),
            file_http_public_base_url=(os.getenv("QQ_FILE_HTTP_PUBLIC_BASE_URL") or "").strip(),
            file_http_ttl_seconds=max(30, _env_int("QQ_FILE_HTTP_TTL_SECONDS", 300)),
            file_base64_max_mb=max(0.1, _env_float("QQ_FILE_BASE64_MAX_MB", 3.0)),
            # 群聊没有长期上下文落盘，所以每次被唤醒时临时拉取最近群消息，帮助模型
            # 理解“刚才大家在聊什么”。设为 0 可以完全关闭，避免额外 NapCat action。
            group_context_messages=max(0, _env_int("QQ_GROUP_CONTEXT_MESSAGES", 50)),
            group_context_max_chars=max(500, _env_int("QQ_GROUP_CONTEXT_MAX_CHARS", 8000)),
        )


def _csv_set(raw: str | None) -> Set[str]:
    return {item.strip() for item in (raw or "").split(",") if item.strip()}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name) or default)
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name) or default)
    except Exception:
        return default


__all__ = ["QQConfig"]
