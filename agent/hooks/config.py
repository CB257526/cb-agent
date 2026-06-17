"""Hook 配置数据结构与加载。

配置文件位于项目级 ``.cbagent/hooks.json``，与现有 ``.cbagent/permissions.json``
平级。结构对齐 Claude Code：

    {
      "hooks": {
        "<事件名>": [
          {
            "matcher": "bash|file_edit",
            "hooks": [
              { "type": "command", "command": "...", "timeout": 60, "shell": null }
            ]
          }
        ]
      }
    }

加载策略偏宽容：文件缺失、JSON 解析失败、字段类型不符都不抛异常，记 warning
后退化成「该部分为空」，保证坏配置不会拖垮 agent 启动（hooks 是可选增强能力）。
第一版只支持 ``type == "command"``，其它类型在加载期直接跳过并记 warning。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# 第一版支持的 hook 事件名（用户指定的 8 个里有真实触发点的 6 个）。
# 配置里出现其它事件名会被记 warning 并跳过，避免拼写错误静默失效。
SUPPORTED_EVENTS = frozenset({
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "Stop",
})

# 第一版支持的 handler 类型。
SUPPORTED_HANDLER_TYPES = frozenset({"command"})

# command handler 默认超时（秒）。
DEFAULT_TIMEOUT = 60.0


@dataclass(frozen=True)
class HookHandler:
    """单个 hook 处理器（第一版仅 command 类型）。"""

    type: str = "command"
    command: str = ""
    timeout: float = DEFAULT_TIMEOUT
    shell: Optional[str] = None      # None=跟随系统；"bash"/"powershell"


@dataclass(frozen=True)
class HookGroup:
    """一个 matcher 组，含若干 handler。"""

    matcher: str = "*"
    handlers: List[HookHandler] = field(default_factory=list)


# 加载结果：事件名 -> [HookGroup]
HooksConfig = Dict[str, List[HookGroup]]


def load_hooks_config(path: Path) -> HooksConfig:
    """读 ``.cbagent/hooks.json``，返回事件名到 HookGroup 列表的映射。

    缺失/解析失败/结构非法都返回尽量多的有效部分（坏的部分跳过），绝不抛异常。
    """
    if not path.exists():
        logger.info("hooks 配置不存在，hooks 功能关闭: path=%s", path)
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("hooks 配置解析失败，hooks 功能关闭: path=%s error=%s", path, e)
        return {}

    if not isinstance(raw, dict):
        logger.warning("hooks 配置根节点不是对象，已忽略: path=%s", path)
        return {}

    hooks_node = raw.get("hooks")
    if not isinstance(hooks_node, dict):
        logger.warning("hooks 配置缺少合法 'hooks' 对象，已忽略: path=%s", path)
        return {}

    config: HooksConfig = {}
    for event_name, groups_raw in hooks_node.items():
        if event_name not in SUPPORTED_EVENTS:
            logger.warning("hooks 配置出现不支持的事件名，已跳过: event=%s", event_name)
            continue
        groups = _parse_groups(event_name, groups_raw)
        if groups:
            config[event_name] = groups

    logger.info("hooks 配置加载完成: events=%s", sorted(config.keys()))
    return config


def _parse_groups(event_name: str, groups_raw: Any) -> List[HookGroup]:
    """解析某事件下的 matcher 组列表。"""
    if not isinstance(groups_raw, list):
        logger.warning("hooks 事件值不是数组，已跳过: event=%s", event_name)
        return []

    groups: List[HookGroup] = []
    for group_raw in groups_raw:
        if not isinstance(group_raw, dict):
            logger.warning("hooks matcher 组不是对象，已跳过: event=%s", event_name)
            continue
        matcher = group_raw.get("matcher", "*")
        if not isinstance(matcher, str):
            matcher = "*"
        handlers = _parse_handlers(event_name, group_raw.get("hooks"))
        if handlers:
            groups.append(HookGroup(matcher=matcher, handlers=handlers))
    return groups


def _parse_handlers(event_name: str, handlers_raw: Any) -> List[HookHandler]:
    """解析某 matcher 组下的 handler 列表。"""
    if not isinstance(handlers_raw, list):
        logger.warning("hooks 组缺少合法 'hooks' 数组，已跳过: event=%s", event_name)
        return []

    handlers: List[HookHandler] = []
    for h in handlers_raw:
        if not isinstance(h, dict):
            continue
        htype = h.get("type", "command")
        if htype not in SUPPORTED_HANDLER_TYPES:
            logger.warning(
                "hooks handler 类型暂不支持，已跳过: event=%s type=%s",
                event_name, htype,
            )
            continue
        command = h.get("command", "")
        if not isinstance(command, str) or not command.strip():
            logger.warning("hooks command handler 缺少 command，已跳过: event=%s", event_name)
            continue
        timeout = h.get("timeout", DEFAULT_TIMEOUT)
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        shell = h.get("shell")
        if shell is not None and not isinstance(shell, str):
            shell = None
        handlers.append(HookHandler(
            type="command",
            command=command,
            timeout=timeout,
            shell=shell,
        ))
    return handlers


__all__ = [
    "HookHandler",
    "HookGroup",
    "HooksConfig",
    "load_hooks_config",
    "SUPPORTED_EVENTS",
    "SUPPORTED_HANDLER_TYPES",
    "DEFAULT_TIMEOUT",
]
