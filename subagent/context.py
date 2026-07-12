"""子代理工具调用所需的父会话运行时上下文。"""

from __future__ import annotations

from contextvars import ContextVar, Token


_CURRENT_PARENT_SESSION_ID: ContextVar[str] = ContextVar(
    "cbagent_subagent_parent_session_id",
    default="",
)


def set_current_parent_session_id(session_id: str) -> Token[str]:
    """绑定当前工具调用所属的父会话 ID。"""

    return _CURRENT_PARENT_SESSION_ID.set(str(session_id or "").strip())


def reset_current_parent_session_id(token: Token[str]) -> None:
    """恢复进入当前会话前的父会话上下文。"""

    _CURRENT_PARENT_SESSION_ID.reset(token)


def get_current_parent_session_id() -> str:
    """返回当前工具调用所属的父会话 ID。"""

    return _CURRENT_PARENT_SESSION_ID.get()


__all__ = [
    "get_current_parent_session_id",
    "reset_current_parent_session_id",
    "set_current_parent_session_id",
]
