"""Section 注册表 —— SystemPromptSection 数据类与 resolve 流程。

对应 claude-code/src/constants/systemPromptSections.ts。

核心抽象：
- SystemPromptSection: 一个"按需计算的字符串生产单元",承载 name + compute。
- system_prompt_section: 工厂函数,产出可缓存的 Section。
- DANGEROUS_uncached_system_prompt_section: 显式标记每轮重算的 Section,
  reason 强制传入(文档化破坏缓存的代价)。
- resolve_system_prompt_sections: 并发 resolve 一组 Section,命中缓存的不调
  compute,返回过滤掉 None 的 list[str]。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Sequence, Union

from .cache import SystemPromptSectionCache, _MISSING


ComputeFn = Callable[[], Union[Awaitable[Optional[str]], Optional[str]]]


@dataclass(frozen=True)
class SystemPromptSection:
    """一个 Section 是一个延迟计算的字符串单元。

    name 是缓存键,在一个 prompt 内必须唯一(组装时检查)。
    compute 可以是 sync 或 async,sync 函数会被 to_thread 包到 event loop。
    cache_break=True 时永远不读不写缓存(对应 DANGEROUS_uncached_*)。
    """

    name: str
    compute: ComputeFn
    cache_break: bool = False


def system_prompt_section(name: str, compute: ComputeFn) -> SystemPromptSection:
    """创建一个可缓存的 Section。

    Section 在 SystemPromptSectionCache 里按 name 存,/clear 与 /compact 失效。
    适合 env_info、token_budget 等"同一进程内多次调用结果稳定"的场景。
    CLAUDE.md 记忆会在运行中被工具更新,必须走 uncached section 实时重读。
    """
    return SystemPromptSection(name=name, compute=compute, cache_break=False)


def DANGEROUS_uncached_system_prompt_section(  # noqa: N802 — 故意大写警示
    name: str,
    compute: ComputeFn,
    *,
    reason: str,
) -> SystemPromptSection:
    """创建一个每轮都重算的 Section。

    破坏 Section 缓存意味着这一段在每次 prompt 组装时都会触发 compute,
    Provider 端的 prompt cache 也会因此失效。reason 强制传入是为了让
    任何加这种 Section 的人都必须显式说明代价。

    典型用例: MCP 服务的 `instructions` 字段会在 connect/disconnect 间
    变化,无法用稳定的 cache key 表达。
    """
    if not reason or not reason.strip():
        raise ValueError(
            f"DANGEROUS_uncached_system_prompt_section({name!r}) requires non-empty reason"
        )
    return SystemPromptSection(name=name, compute=compute, cache_break=True)


async def _run_compute(compute: ComputeFn) -> Optional[str]:
    """统一调用 sync / async compute。

    sync 函数走 asyncio.to_thread,避免阻塞事件循环;async 函数直接 await。
    任何异常都向上抛,由 resolve_system_prompt_sections 决定是否吞掉。
    """
    if inspect.iscoroutinefunction(compute):
        return await compute()
    result = await asyncio.to_thread(compute)
    if inspect.isawaitable(result):
        return await result
    return result


async def resolve_system_prompt_sections(
    sections: Sequence[SystemPromptSection],
    cache: SystemPromptSectionCache,
) -> list[str]:
    """并发 resolve 一组 Section,返回过滤掉 None 与空串的 list[str]。

    - cache_break=True 的 Section 每次重算,且不写入缓存。
    - 普通 Section 命中缓存时直接返回,跳过 compute。
    - 重名 Section 在同一次调用中会得到一致结果(共享缓存键)。
    """
    seen_names: dict[str, int] = {}
    for s in sections:
        seen_names[s.name] = seen_names.get(s.name, 0) + 1

    async def _resolve_one(section: SystemPromptSection) -> Optional[str]:
        if section.cache_break:
            return await _run_compute(section.compute)
        cached = cache.get(section.name)
        if cached is not _MISSING:
            return cached  # type: ignore[return-value]
        value = await _run_compute(section.compute)
        cache.set(section.name, value)
        return value

    results: list[Optional[str]] = await asyncio.gather(
        *(_resolve_one(s) for s in sections),
        return_exceptions=False,
    )
    return [r for r in results if r is not None and r.strip()]


__all__ = [
    "SystemPromptSection",
    "system_prompt_section",
    "DANGEROUS_uncached_system_prompt_section",
    "resolve_system_prompt_sections",
]
