"""压缩摘要协议 + 默认实现。

对应 claude-code 中 sessionMemoryCompact / API summary 的摘要生成路径。

cb-agent 已有的 work_context.TraceSummarizer 是工具轨迹摘要,不直接复用;
这里定义一个独立的"对话压缩摘要器"协议,具体实现可以由 session 注入。
"""

from __future__ import annotations

from typing import List, Optional, Protocol, Sequence

from core.message import Message


class Summarizer(Protocol):
    """对话压缩摘要器协议。

    summarize(messages, focus) -> 摘要文本。
    实现可以是 LLM 调用,也可以是规则拼接。
    返回 None 表示"无法生成摘要,降级处理"。
    """

    async def summarize(
        self,
        messages: Sequence[Message],
        *,
        focus: Optional[str] = None,
    ) -> Optional[str]:
        ...


class RuleBasedSummarizer:
    """规则摘要器: 不调 LLM,直接拼最近若干条 user/assistant 消息标题。

    用作 LLM 摘要不可用时的兜底,保证 /compact 至少能产出可读结果。
    """

    def __init__(self, max_messages: int = 12, max_chars_per_msg: int = 300) -> None:
        self.max_messages = max_messages
        self.max_chars_per_msg = max_chars_per_msg

    async def summarize(
        self,
        messages: Sequence[Message],
        *,
        focus: Optional[str] = None,
    ) -> Optional[str]:
        if not messages:
            return None
        recent = list(messages[-self.max_messages:])
        bullets: List[str] = []
        for m in recent:
            role = m.role.value if hasattr(m.role, "value") else str(m.role)
            content = m.content if isinstance(m.content, str) else _flatten_content(m.content)
            if not content or not content.strip():
                continue
            content = " ".join(content.split())
            if len(content) > self.max_chars_per_msg:
                content = content[: self.max_chars_per_msg - 1].rstrip() + "…"
            bullets.append(f"- ({role}) {content}")
        if not bullets:
            return None
        header = "前序对话摘要(规则压缩,无 LLM 调用):"
        if focus:
            header = f"{header} 关注主题 = {focus}"
        return header + "\n" + "\n".join(bullets)


def _flatten_content(content) -> str:
    """把多模态 content 数组拍扁成一行可读文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    parts.append("[image]")
                elif item.get("type") == "audio_url":
                    parts.append("[audio]")
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p)
    return str(content)


__all__ = ["RuleBasedSummarizer", "Summarizer"]
