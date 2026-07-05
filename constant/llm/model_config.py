"""Unified model provider configuration.

The runtime still supports the old environment variables, but this module lets
users keep multiple OpenAI-compatible providers in one JSON/JSON5 file and
switch between them without rebuilding AgentSession history.
"""

from __future__ import annotations

import json
import os
import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from constant.llm.constant_llm import ConstantLLM, _parse_bool_env, _parse_token_count_env


CONFIG_ENV = "CBAGENT_MODEL_CONFIG"
DEFAULT_CONFIG_CANDIDATES = (
    ".cbagent/models.json",
    "models.json",
)


def _load_json_like(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        json_error = None
        try:
            import json5  # type: ignore
            return json5.loads(text)
        except Exception as exc:
            json_error = exc
        try:
            # Users often paste Python-like examples with True/False. This accepts
            # only literals, not arbitrary code.
            return ast.literal_eval(text)
        except Exception as exc:  # pragma: no cover - only hit when optional dep missing
            raise ValueError(f"{path} is not valid JSON/JSON5/Python literal") from (json_error or exc)


def _as_bool(value: Any, default: Optional[bool] = None) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    parsed = _parse_bool_env(None if value is None else str(value))
    return default if parsed is None else parsed


def _as_token_count(value: Any) -> Optional[int]:
    if isinstance(value, int) and value > 0:
        return value
    return _parse_token_count_env(None if value is None else str(value))


def _first_str(data: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str):
            return value.strip()
    return ""


@dataclass(frozen=True)
class ModelProvider:
    key: str
    name: str
    base_url: str
    api_key: str


@dataclass(frozen=True)
class ModelChoice:
    key: str
    provider_key: str
    provider_name: str
    model_id: str
    display_name: str
    base_url: str
    api_key: str
    is_tool: bool
    is_reasoning: bool
    max_tokens: int
    image_ability: bool

    def capability_config(self) -> Dict[str, Any]:
        return {
            "is_tool": self.is_tool,
            "is_reasoning": self.is_reasoning,
            "max_tokens": self.max_tokens,
            "image_ability": self.image_ability,
        }

    def public_dict(self, *, current: bool = False) -> Dict[str, Any]:
        return {
            "key": self.key,
            "provider": self.provider_name,
            "model": self.model_id,
            "name": self.display_name,
            "current": current,
            "is_tool": self.is_tool,
            "is_reasoning": self.is_reasoning,
            "max_tokens": self.max_tokens,
            "image_ability": self.image_ability,
            "base_url": self.base_url,
        }


class ModelConfigManager:
    """Loads configured providers and exposes sanitized choices for UI/RPC."""

    def __init__(self, path: Optional[Path], providers: List[ModelProvider], choices: List[ModelChoice]):
        self.path = path
        self.providers = providers
        self.choices = choices
        self._by_key = {choice.key: choice for choice in choices}
        # First model id wins. If a config intentionally duplicates model ids across
        # providers, UI/RPC should use the unique key instead.
        self._by_model: Dict[str, ModelChoice] = {}
        for choice in choices:
            self._by_model.setdefault(choice.model_id, choice)
        self.register_capabilities()

    @classmethod
    def load(cls, project_root: Optional[Path] = None) -> "ModelConfigManager":
        path = cls.resolve_config_path(project_root)
        if path is None:
            return cls.from_environment()
        raw = _load_json_like(path)
        providers = list(_iter_provider_dicts(raw))
        return cls.from_provider_dicts(path, providers)

    @classmethod
    def from_environment(cls) -> "ModelConfigManager":
        model = (os.getenv("LLM_MODEL_ID") or "").strip()
        base_url = (os.getenv("LLM_BASE_URL") or "").strip()
        api_key = (os.getenv("LLM_API_KEY") or "").strip()
        if not model:
            return cls(None, [], [])
        provider = ModelProvider(
            key="env",
            name="Env",
            base_url=base_url,
            api_key=api_key,
        )
        choice = _build_choice(
            provider=provider,
            model_id=model,
            model_data={},
            index=0,
        )
        return cls(None, [provider], [choice])

    @classmethod
    def from_provider_dicts(cls, path: Path, provider_dicts: Iterable[Dict[str, Any]]) -> "ModelConfigManager":
        providers: List[ModelProvider] = []
        choices: List[ModelChoice] = []
        seen_provider_keys: set[str] = set()
        for provider_index, data in enumerate(provider_dicts):
            options = data.get("options") if isinstance(data.get("options"), dict) else {}
            provider_name = _first_str(data, "name", "provider", "id") or f"Provider {provider_index + 1}"
            provider_key = _unique_key(_slug(provider_name) or f"provider-{provider_index + 1}", seen_provider_keys)
            seen_provider_keys.add(provider_key)
            provider = ModelProvider(
                key=provider_key,
                name=provider_name,
                base_url=(
                    _first_str(options, "baseURL", "baseUrl", "base_url")
                    or _first_str(data, "baseURL", "baseUrl", "base_url")
                ),
                api_key=(
                    _first_str(options, "apiKey", "api_key")
                    or _first_str(data, "apiKey", "api_key")
                ),
            )
            providers.append(provider)
            models = data.get("models")
            if not isinstance(models, dict):
                continue
            for model_index, (model_id, model_data) in enumerate(models.items()):
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                if not isinstance(model_data, dict):
                    model_data = {}
                choices.append(_build_choice(
                    provider=provider,
                    model_id=model_id.strip(),
                    model_data=model_data,
                    index=model_index,
                ))
        return cls(path, providers, choices)

    @staticmethod
    def resolve_config_path(project_root: Optional[Path] = None) -> Optional[Path]:
        env_path = (os.getenv(CONFIG_ENV) or "").strip()
        if env_path:
            path = Path(env_path).expanduser()
            if not path.is_absolute() and project_root is not None:
                path = project_root / path
            return path if path.exists() else None

        roots: List[Path] = []
        if project_root is not None:
            roots.append(project_root)
        package_root = Path(__file__).resolve().parents[2]
        roots.append(package_root)

        seen: set[Path] = set()
        for root in roots:
            root = root.resolve()
            if root in seen:
                continue
            seen.add(root)
            for rel in DEFAULT_CONFIG_CANDIDATES:
                path = root / rel
                if path.exists():
                    return path
        return None

    def register_capabilities(self) -> None:
        for choice in self.choices:
            ConstantLLM.llm_dict[choice.model_id] = {
                **ConstantLLM.llm_dict.get(choice.model_id, {}),
                **choice.capability_config(),
            }

    def first_choice(self) -> Optional[ModelChoice]:
        return self.choices[0] if self.choices else None

    def find(self, key_or_model: str) -> Optional[ModelChoice]:
        value = (key_or_model or "").strip()
        if not value:
            return None
        return self._by_key.get(value) or self._by_model.get(value)

    def find_by_model(self, model_id: str) -> Optional[ModelChoice]:
        return self._by_model.get((model_id or "").strip())

    def public_models(self, current_key: Optional[str] = None, current_model: Optional[str] = None) -> List[Dict[str, Any]]:
        current_key = current_key or ""
        current_model = current_model or ""
        return [
            choice.public_dict(current=choice.key == current_key or choice.model_id == current_model)
            for choice in self.choices
        ]


def _iter_provider_dicts(raw: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                yield item
        return
    if not isinstance(raw, dict):
        return
    providers = raw.get("providers")
    if isinstance(providers, list):
        for item in providers:
            if isinstance(item, dict):
                yield item
        return
    # Single-provider shape:
    # {"name": "Kimi", "options": {...}, "models": {...}}
    if isinstance(raw.get("models"), dict):
        yield raw


def _build_choice(provider: ModelProvider, model_id: str, model_data: Dict[str, Any], index: int) -> ModelChoice:
    registered = ConstantLLM.llm_dict.get(model_id, {})
    display_name = _first_str(model_data, "name", "displayName", "display_name") or model_id
    is_tool = _as_bool(model_data.get("is_tool"), registered.get("is_tool") if isinstance(registered, dict) else None)
    is_reasoning = _as_bool(
        model_data.get("is_reasoning"),
        registered.get("is_reasoning") if isinstance(registered, dict) else None,
    )
    image_ability = _as_bool(
        model_data.get("image_ability"),
        registered.get("image_ability") if isinstance(registered, dict) else None,
    )
    max_tokens = (
        _as_token_count(model_data.get("max_input_tokens"))
        or _as_token_count(model_data.get("max_tokens"))
        or _as_token_count(registered.get("max_input_tokens") if isinstance(registered, dict) else None)
        or _as_token_count(registered.get("max_tokens") if isinstance(registered, dict) else None)
        or ConstantLLM.DEFAULT_MAX_TOKENS
    )
    key = f"{provider.key}:{model_id}" if provider.key else f"{index}:{model_id}"
    return ModelChoice(
        key=key,
        provider_key=provider.key,
        provider_name=provider.name,
        model_id=model_id,
        display_name=display_name,
        base_url=provider.base_url,
        api_key=provider.api_key,
        is_tool=True if is_tool is None else bool(is_tool),
        is_reasoning=False if is_reasoning is None else bool(is_reasoning),
        max_tokens=max_tokens,
        image_ability=False if image_ability is None else bool(image_ability),
    )


def _slug(value: str) -> str:
    clean = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    clean = "-".join(part for part in clean.split("-") if part)
    return clean


def _unique_key(base: str, seen: set[str]) -> str:
    if base not in seen:
        return base
    idx = 2
    while f"{base}-{idx}" in seen:
        idx += 1
    return f"{base}-{idx}"


__all__ = [
    "CONFIG_ENV",
    "ModelChoice",
    "ModelConfigManager",
    "ModelProvider",
]
