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

    @classmethod
    def from_env(cls) -> "QQConfig":
        mode = (os.getenv("QQ_GROUP_MODE") or "mention").strip().lower()
        if mode not in {"mention", "prefix", "all"}:
            mode = "mention"
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
