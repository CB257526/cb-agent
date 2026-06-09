"""微信 OC transport 配置读取。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class WeChatConfig:
    """微信 OC 配置。

    默认值参考 openclaw-weixin 与 AstrBot 的个人微信适配器。token/account_id 可以
    从环境变量提供，也可以由扫码登录后写入 state_file，下次启动自动恢复。
    """

    enabled: bool = True
    base_url: str = "https://ilinkai.weixin.qq.com"
    cdn_base_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    token: str = ""
    account_id: str = ""
    bot_type: str = "3"
    api_timeout_ms: int = 15_000
    long_poll_timeout_ms: int = 35_000
    qr_poll_interval_seconds: float = 1.0
    state_file: Path = field(default_factory=lambda: Path(".cbagent/wechat/state.json"))
    attachment_dir: Path = field(default_factory=lambda: Path(".cbagent/platform_attachments/wechat"))
    action_timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "WeChatConfig":
        state_file = Path(os.getenv("WECHAT_STATE_FILE") or ".cbagent/wechat/state.json").expanduser()
        attachment_dir = Path(os.getenv("CBAGENT_PLATFORM_ATTACHMENT_DIR_WECHAT") or ".cbagent/platform_attachments/wechat").expanduser()
        return cls(
            enabled=_env_bool("WECHAT_ENABLE", True),
            base_url=(os.getenv("WECHAT_BASE_URL") or "https://ilinkai.weixin.qq.com").strip().rstrip("/"),
            cdn_base_url=(os.getenv("WECHAT_CDN_BASE_URL") or "https://novac2c.cdn.weixin.qq.com/c2c").strip().rstrip("/"),
            token=(os.getenv("WECHAT_TOKEN") or "").strip(),
            account_id=(os.getenv("WECHAT_ACCOUNT_ID") or "").strip(),
            bot_type=(os.getenv("WECHAT_BOT_TYPE") or "3").strip() or "3",
            api_timeout_ms=max(1000, _env_int("WECHAT_API_TIMEOUT_MS", 15_000)),
            long_poll_timeout_ms=max(1000, _env_int("WECHAT_LONG_POLL_TIMEOUT_MS", 35_000)),
            qr_poll_interval_seconds=max(0.2, _env_float("WECHAT_QR_POLL_INTERVAL_SECONDS", 1.0)),
            state_file=state_file,
            attachment_dir=attachment_dir,
            action_timeout_seconds=max(1.0, _env_float("WECHAT_ACTION_TIMEOUT_SECONDS", 30.0)),
        )


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


__all__ = ["WeChatConfig"]
