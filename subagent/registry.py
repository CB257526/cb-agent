"""子代理角色注册表。"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from subagent.list import BUILTIN_SUBAGENTS
from subagent.models import (
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_TYPE,
    SubagentDefinition,
    SubagentPermissionPolicy,
    clip_text,
    safe_name,
)


logger = logging.getLogger(__name__)


class SubagentRegistry:
    """加载内置角色和 Markdown 自定义角色。

    覆盖顺序固定为：内置 < 用户目录 < 项目目录。用户自定义文件只描述数据，
    不导入 Python 代码，避免项目内容在启动阶段获得任意代码执行能力。
    """

    def __init__(self, workspace_dir: Path, user_agents_dir: Optional[Path] = None) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.user_agents_dir = Path(user_agents_dir or (Path.home() / ".cbagent" / "agents"))
        self.project_agents_dir = self.workspace_dir / ".cbagent" / "agents"
        self._lock = threading.RLock()
        self._definitions: Dict[str, SubagentDefinition] = {}
        self._errors: List[Dict[str, str]] = []
        self.refresh()

    def refresh(self) -> None:
        """重新加载全部角色；单个自定义文件失败不会清空其它有效角色。"""

        definitions = {item.name: item for item in BUILTIN_SUBAGENTS}
        errors: List[Dict[str, str]] = []
        for directory in (self.user_agents_dir, self.project_agents_dir):
            for path in self._iter_definition_files(directory):
                try:
                    item = self._load_file(path)
                    definitions[item.name] = item
                except Exception as exc:  # noqa: BLE001
                    logger.exception("加载子代理定义失败: %s", path)
                    errors.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
        with self._lock:
            self._definitions = definitions
            self._errors = errors

    def get(self, name: Optional[str]) -> SubagentDefinition:
        """按名称返回角色；未知名称明确报错，避免权限角色静默降级。"""

        raw = str(name or DEFAULT_SUBAGENT_TYPE).strip()
        key = safe_name(raw)
        # 兼容旧名称和常见角色叫法，但公开列表始终只展示规范名称。
        key = {
            "general-purpose": DEFAULT_SUBAGENT_TYPE,
            "explorer": "explore",
            "explored": "explore",
            "review": "reviewer",
        }.get(key, key)
        with self._lock:
            found = self._definitions.get(key)
            available = sorted(self._definitions)
        if found is None:
            raise ValueError(
                f"未知 subagent_type: {raw!r}；可用角色: {', '.join(available)}"
            )
        return found

    def list(self) -> List[SubagentDefinition]:
        with self._lock:
            return [self._definitions[name] for name in sorted(self._definitions)]

    def errors(self) -> List[Dict[str, str]]:
        """返回最近一次刷新中无效自定义定义的诊断。"""

        with self._lock:
            return [dict(item) for item in self._errors]

    @staticmethod
    def _iter_definition_files(directory: Path) -> Iterable[Path]:
        if not directory.exists():
            return ()
        return tuple(sorted(directory.glob("*.md")))

    def _load_file(self, path: Path) -> SubagentDefinition:
        text = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        if not isinstance(meta, dict):
            raise ValueError("frontmatter 必须是 YAML 对象")

        name = safe_name(str(meta.get("name") or path.stem))
        description = str(meta.get("description") or _infer_description(body) or name).strip()
        system_prompt = body.strip()
        if not system_prompt:
            raise ValueError("角色提示词正文不能为空")

        tools = _parse_tools(meta.get("tools")) if "tools" in meta else None
        if "tools" not in meta:
            # 自定义角色省略 tools 时采用最小只读集合，不能因为漏配而继承主 Agent
            # 的全部原生/MCP/平台工具。
            tools = ("file_read", "glob", "grep", "ls")
        elif tools is None:
            # 显式 null/空值采用无工具集合，不能回退成继承全部工具。
            tools = ()
        max_turns = _parse_positive_int(meta.get("max_turns", meta.get("max-turns")), DEFAULT_SUBAGENT_MAX_TURNS)
        permissions = _parse_permissions(meta.get("permissions"), meta)
        return SubagentDefinition(
            name=name,
            description=description,
            system_prompt=system_prompt,
            tools=tools,
            max_turns=max_turns,
            permissions=permissions,
            source_path=str(path.resolve()),
            builtin=False,
        )


def _split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """使用安全 YAML 解析 Markdown frontmatter。"""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("frontmatter 缺少结束分隔符")
    raw_meta = normalized[4:end]
    loaded = yaml.safe_load(raw_meta) if raw_meta.strip() else {}
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("frontmatter 必须是 YAML 对象")
    return loaded, normalized[end + len("\n---\n") :]


def _infer_description(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return clip_text(stripped, 160)
    return ""


def _parse_tools(value: Any) -> Optional[Tuple[str, ...]]:
    if value is None:
        return None
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raise ValueError("tools 必须是字符串或字符串数组")
    names = tuple(dict.fromkeys(safe_name(str(item), default="") for item in raw_items if str(item).strip()))
    names = tuple(name for name in names if name)
    return names


def _parse_positive_int(value: Any, default: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_turns 必须是正整数") from exc
    if parsed < 1:
        raise ValueError("max_turns 必须大于 0")
    return parsed


def _parse_permissions(value: Any, meta: Dict[str, Any]) -> SubagentPermissionPolicy:
    raw = value if isinstance(value, dict) else {}
    if value is not None and not isinstance(value, dict):
        raise ValueError("permissions 必须是 YAML 对象")

    bash_mode = str(raw.get("bash_mode", meta.get("bash_mode", "deny"))).strip().lower()
    if bash_mode not in {"deny", "read_only", "inherit"}:
        raise ValueError("permissions.bash_mode 必须是 deny/read_only/inherit")
    denied = _parse_tools(raw.get("denied_tools")) or ()
    return SubagentPermissionPolicy(
        bash_mode=bash_mode,
        workspace_write=_as_bool(raw.get("workspace_write", meta.get("workspace_write", False))),
        external_paths=_as_bool(raw.get("external_paths", meta.get("external_paths", False))),
        # 首版全局禁止嵌套派生。即使自定义配置写 true，也在这里收紧为 false。
        allow_spawn=False,
        denied_tools=denied,
    )


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


__all__ = ["SubagentRegistry"]
