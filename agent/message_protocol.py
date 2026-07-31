"""Canonical history 的工具调用协议校验。

正常请求不允许在发送前删除、补写或重排历史。这里仅验证
``assistant.tool_calls`` 与随后 ``tool`` 消息是否完整配对；一旦发现损坏就明确
失败并保留 journal 现场。旧会话迁移所需的兼容清理由迁移器私有实现，不进入
正常请求链路。
"""

from __future__ import annotations

from typing import Any, Dict, List


def validate_tool_protocol(
    messages: List[Dict[str, Any]],
    *,
    allow_pending_tail: bool = False,
) -> None:
    """验证 assistant.tool_calls 与紧随其后的 tool 结果完整配对。

    canonical history 的正常请求禁止在组装阶段修剪消息。发现损坏时直接失败，
    让调用方保留原始 journal 现场；只有一次性旧会话迁移仍可使用上面的兼容清理。
    """

    pending: set[str] = set()
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"消息协议项不是对象: index={index}")
        role = str(message.get("role") or "")
        if pending:
            if role != "tool":
                raise ValueError(
                    "assistant.tool_calls 后缺少完整 tool 结果: "
                    f"index={index} pending={sorted(pending)}"
                )
            call_id = str(message.get("tool_call_id") or "")
            if call_id not in pending:
                raise ValueError(
                    f"tool_call_id 未在上一条 assistant 中声明: {call_id!r}"
                )
            pending.remove(call_id)
            continue

        if role == "tool":
            raise ValueError(
                f"发现没有父 assistant.tool_calls 的 tool 消息: index={index}"
            )
        if role == "assistant" and message.get("tool_calls"):
            call_ids = [
                str(call.get("id") or "")
                for call in message.get("tool_calls") or []
                if isinstance(call, dict)
            ]
            if not call_ids or any(not call_id for call_id in call_ids):
                raise ValueError(f"assistant.tool_calls 缺少稳定 id: index={index}")
            if len(call_ids) != len(set(call_ids)):
                raise ValueError(f"assistant.tool_calls 含重复 id: index={index}")
            pending = set(call_ids)

    if pending and not allow_pending_tail:
        raise ValueError(
            f"历史末尾存在未配对工具调用: pending={sorted(pending)}"
        )


__all__ = [
    "validate_tool_protocol",
]
