"""消息协议合法化 —— 清理孤儿 tool 消息。

背景:
- CC 对齐重构后,self.history 累积的是原始协议消息:
  user → assistant(tool_calls=[a,b]) → tool(a) → tool(b) → assistant(final) → ...
- _build_chat_messages 会先按 compact_boundary 切片,再用 history_window 取尾部
  N 条。这一刀可能正好落在 assistant(tool_calls) 和它的 tool 响应之间,导致切片
  开头出现"孤儿 tool 消息"——它的父 assistant.tool_calls 被切掉了。
- OpenAI 兼容协议(DeepSeek 等)对此会直接报错:
  "messages with role 'tool' must be a response to a preceding message with
  'tool_calls'"。跨进程恢复(_trim_restored_history)截断时也有同样风险。

这里的算法对两种载体各提供一份实现:
- dict 版服务 _build_chat_messages 切片后的 OpenAI dict messages;
- Message 版服务 work_context 的跨进程 history 恢复。

判定规则一致:从前往后扫,assistant.tool_calls 声明的 id 进入"已见"集合;
role=tool 消息只有当它的 tool_call_id 已在集合里才保留,否则丢弃。这样既能
清掉开头的孤儿,也能防御中间任何配对断裂的 tool 消息。

注意:这一层只丢"无父"的 tool 消息,不处理"有父无子"(assistant.tool_calls
声明了某个 id 但没有对应 tool 响应)。后者在本项目的累积逻辑里不会发生——
_tool_loop 内 assistant.tool_calls 之后必定 append 全部 tool 响应,而 tool
响应总在 assistant 之后,window 截断不会留下 assistant 却切掉它后面的 tool。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.message import Message

logger = logging.getLogger(__name__)


def _role_of(message: Any) -> str:
    """统一取 role 字符串,兼容 dict 与 Message(role 可能是 Enum)。"""
    if isinstance(message, dict):
        return str(message.get("role") or "")
    role = getattr(message, "role", "")
    return role.value if hasattr(role, "value") else str(role)


def drop_orphan_tool_messages(messages: List[Dict[str, Any]]) -> int:
    """原地丢弃孤儿 tool 消息(dict 版)。返回被丢弃的条数。

    用于 _build_chat_messages:切片 + window 截断后、microcompact 之前调用,
    保证发给 LLM 的请求体里每条 role=tool 都能在前文找到声明它 tool_call_id
    的 assistant.tool_calls。
    """
    if not messages:
        return 0

    seen_call_ids: set[str] = set()
    keep: List[Dict[str, Any]] = []
    dropped = 0
    for msg in messages:
        if not isinstance(msg, dict):
            keep.append(msg)
            continue
        role = msg.get("role")
        if role == "assistant":
            for tc in (msg.get("tool_calls") or []):
                cid = tc.get("id") if isinstance(tc, dict) else None
                if cid:
                    seen_call_ids.add(str(cid))
            keep.append(msg)
        elif role == "tool":
            cid = msg.get("tool_call_id")
            if cid and str(cid) in seen_call_ids:
                keep.append(msg)
            else:
                dropped += 1
        else:
            keep.append(msg)

    if dropped:
        # 原地替换内容,保持调用方持有的 list 引用不变。
        messages[:] = keep
        logger.info("drop_orphan_tool_messages: dropped %s orphan tool message(s)", dropped)
    return dropped


def drop_orphan_tool_message_objects(messages: List[Message]) -> List[Message]:
    """丢弃孤儿 tool 消息(Message 版)。返回新列表,不修改入参。

    用于 work_context._trim_restored_history:跨进程恢复后按窗口截断 history,
    同样可能把 assistant.tool_calls 切掉而留下它的 tool 响应。
    """
    if not messages:
        return []

    seen_call_ids: set[str] = set()
    out: List[Message] = []
    dropped = 0
    for msg in messages:
        role = _role_of(msg)
        if role == "assistant":
            for tc in (getattr(msg, "tool_calls", None) or []):
                cid = tc.get("id") if isinstance(tc, dict) else None
                if cid:
                    seen_call_ids.add(str(cid))
            out.append(msg)
        elif role == "tool":
            cid = getattr(msg, "tool_call_id", None)
            if cid and str(cid) in seen_call_ids:
                out.append(msg)
            else:
                dropped += 1
        else:
            out.append(msg)

    if dropped:
        logger.info(
            "drop_orphan_tool_message_objects: dropped %s orphan tool message(s)",
            dropped,
        )
    return out


__all__ = [
    "drop_orphan_tool_messages",
    "drop_orphan_tool_message_objects",
]
