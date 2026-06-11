"""Feature gates for heavyweight memory backends.

The default cb-agent memory path is lightweight Markdown memory plus lexical
knowledge lookup. Vector stores, RAG, and embedding models are opt-in because
they can pull in large dependencies or block startup/request preparation.
"""

from __future__ import annotations

import os


FULL_MEMORY_ENV = "CBAGENT_ENABLE_FULL_MEMORY"


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def is_full_memory_enabled() -> bool:
    """Return whether vector/RAG/embedding memory is explicitly enabled."""
    return _truthy(os.getenv(FULL_MEMORY_ENV))


def full_memory_disabled_message() -> str:
    return (
        "Full memory is disabled. Default memory is Markdown + lexical lookup; "
        f"set {FULL_MEMORY_ENV}=1 to enable vector stores, RAG, and embeddings."
    )


__all__ = [
    "FULL_MEMORY_ENV",
    "full_memory_disabled_message",
    "is_full_memory_enabled",
]
