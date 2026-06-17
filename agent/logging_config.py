from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


LOG_LEVEL_ENV = "CBAGENT_LOG_LEVEL"
LOG_DIR_ENV = "CBAGENT_LOG_DIR"

_LEVEL_ALIASES = {
    "basic": "basic",
    "base": "basic",
    "normal": "basic",
    "info": "basic",
    "detail": "detail",
    "detailed": "detail",
    "debug": "detail",
    "full": "full",
    "trace": "full",
    "all": "full",
}


@dataclass(frozen=True)
class LoggingSettings:
    verbosity: str
    log_dir: Path
    system_log_dir: Path
    conversation_log_dir: Path
    runtime_log_path: Path
    message_log_mode: str
    file_level: int
    console_level: int
    project_level: int
    memory_level: int
    third_party_level: int

    @property
    def is_full(self) -> bool:
        return self.verbosity == "full"

    @property
    def is_detail(self) -> bool:
        return self.verbosity in {"detail", "full"}


def normalize_log_verbosity(raw: str | None) -> str:
    value = (raw or "basic").strip().lower()
    return _LEVEL_ALIASES.get(value, "basic")


def resolve_logging_settings(
    *,
    project_root: Path,
    env: Mapping[str, str] | None = None,
    timestamp: int | None = None,
) -> LoggingSettings:
    source = os.environ if env is None else env
    verbosity = normalize_log_verbosity(source.get(LOG_LEVEL_ENV))
    raw_dir = (source.get(LOG_DIR_ENV) or "").strip()
    log_dir = Path(raw_dir) if raw_dir else project_root / ".cbagent" / "logs"
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir

    system_log_dir = log_dir / "system"
    conversation_log_dir = log_dir / "conversations"

    ts = int(time.time() if timestamp is None else timestamp)
    runtime_log_path = system_log_dir / f"cb-agent-{ts}.log"

    if verbosity == "full":
        return LoggingSettings(
            verbosity=verbosity,
            log_dir=log_dir,
            system_log_dir=system_log_dir,
            conversation_log_dir=conversation_log_dir,
            runtime_log_path=runtime_log_path,
            message_log_mode="full",
            file_level=logging.DEBUG,
            console_level=logging.DEBUG,
            project_level=logging.DEBUG,
            memory_level=logging.DEBUG,
            third_party_level=logging.DEBUG,
        )
    if verbosity == "detail":
        return LoggingSettings(
            verbosity=verbosity,
            log_dir=log_dir,
            system_log_dir=system_log_dir,
            conversation_log_dir=conversation_log_dir,
            runtime_log_path=runtime_log_path,
            message_log_mode="full",
            file_level=logging.DEBUG,
            console_level=logging.INFO,
            project_level=logging.DEBUG,
            memory_level=logging.INFO,
            third_party_level=logging.WARNING,
        )
    return LoggingSettings(
        verbosity="basic",
        log_dir=log_dir,
        system_log_dir=system_log_dir,
        conversation_log_dir=conversation_log_dir,
        runtime_log_path=runtime_log_path,
        message_log_mode="full",
        file_level=logging.INFO,
        console_level=logging.WARNING,
        project_level=logging.INFO,
        memory_level=logging.WARNING,
        third_party_level=logging.WARNING,
    )


def configure_logging(project_root: Path) -> LoggingSettings:
    load_dotenv(project_root / ".env")
    settings = resolve_logging_settings(project_root=project_root)
    settings.system_log_dir.mkdir(parents=True, exist_ok=True)
    settings.conversation_log_dir.mkdir(parents=True, exist_ok=True)

    fmt = "%(asctime)s.%(msecs)03d [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)

    file_handler = logging.FileHandler(
        settings.runtime_log_path,
        encoding="utf-8",
    )
    file_handler.setLevel(settings.file_level)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(settings.console_level)
    console_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.DEBUG,
        handlers=[file_handler, console_handler],
        force=True,
    )

    for name in (
        "__main__",
        "run_agent",
        "agent",
        "context",
        "core",
        "skills",
        "tools",
        "utils",
        "constant",
    ):
        logging.getLogger(name).setLevel(settings.project_level)

    logging.getLogger("memory").setLevel(settings.memory_level)

    for name in ("openai", "httpx", "httpcore", "urllib3", "requests", "asyncio"):
        logging.getLogger(name).setLevel(settings.third_party_level)

    logging.getLogger(__name__).info(
        "logging configured: verbosity=%s runtime_log=%s message_log_mode=%s",
        settings.verbosity,
        settings.runtime_log_path,
        settings.message_log_mode,
    )
    return settings


__all__ = [
    "LOG_DIR_ENV",
    "LOG_LEVEL_ENV",
    "LoggingSettings",
    "configure_logging",
    "normalize_log_verbosity",
    "resolve_logging_settings",
]
