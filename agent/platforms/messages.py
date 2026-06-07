"""通讯平台统一消息结构。

设计目标是让 QQ/NapCat、未来微信等平台复用同一套 Agent 入口与事件出口。
平台适配器负责协议细节，Agent 和工具只看到这些平台无关的数据类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ConversationKey:
    """唯一标识一个通讯软件会话。

    ``platform`` 用来区分 qq/wechat 等平台，``kind`` 用来区分 private/group，
    ``id`` 是平台侧会话 ID。后续做多平台并发时，这个结构可以直接作为路由键。
    """

    platform: str
    kind: str
    id: str

    @property
    def stable_id(self) -> str:
        return f"{self.platform}:{self.kind}:{self.id}"

    def to_dict(self) -> Dict[str, str]:
        return {"platform": self.platform, "kind": self.kind, "id": self.id}


@dataclass
class InboundAttachment:
    """通讯软件收到的附件。

    ``path`` 是已经保存到本地的文件路径；如果平台只给 URL 且下载失败，则由适配器
    把 URL 放进 ``description``，不要假装存在本地文件。
    """

    modality: str
    path: Optional[str] = None
    url: Optional[str] = None
    file_id: Optional[str] = None
    file_name: str = ""
    description: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_prompt_attachment(self) -> Optional[Dict[str, Any]]:
        if not self.path:
            return None
        return {
            "path": self.path,
            "modality": self.modality,
            "source": "direct",
        }


@dataclass
class InboundMessage:
    """平台入站消息，供 Agent 会话入口消费。"""

    conversation: ConversationKey
    sender_id: str
    sender_name: str
    text: str
    raw: Dict[str, Any] = field(default_factory=dict)
    attachments: List[InboundAttachment] = field(default_factory=list)
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None

    def prompt_text(self) -> str:
        """生成传给 Agent 的用户文本。

        保留最少的平台上下文，帮助模型知道消息来自哪里；附件 URL 下载失败时也在这里
        做显式提示，避免模型误以为自己已经看到了图片/文件内容。
        """

        header = (
            f"[通讯软件消息 platform={self.conversation.platform} "
            f"conversation={self.conversation.kind}:{self.conversation.id} "
            f"sender={self.sender_name or self.sender_id}]"
        )
        parts = [header, self.text.strip()]
        if self.reply_to_message_id:
            parts.append(f"[引用消息] message_id={self.reply_to_message_id}")
        for item in self.attachments:
            if item.path:
                continue
            desc = item.description or item.url
            if desc:
                parts.append(f"[附件提示] {item.modality}: {desc}")
        return "\n".join(p for p in parts if p).strip()

    def prompt_attachments(self) -> List[Dict[str, Any]]:
        return [
            payload
            for item in self.attachments
            for payload in [item.to_prompt_attachment()]
            if payload is not None
        ]


@dataclass
class OutboundSegment:
    """平台无关出站消息段。

    ``kind`` 支持 text/image/audio/video/file/sticker/question/todo/status。平台不支持
    某种段时，应降级成 text/status，而不是让 Agent 主流程报错。
    """

    kind: str
    text: str = ""
    path: str = ""
    file_name: str = ""
    mime_type: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def text_segment(cls, text: str, *, kind: str = "text") -> "OutboundSegment":
        return cls(kind=kind, text=text)

    @classmethod
    def file_segment(
        cls,
        *,
        kind: str,
        path: str,
        file_name: str = "",
        text: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "OutboundSegment":
        return cls(
            kind=kind,
            text=text,
            path=str(Path(path)),
            file_name=file_name or Path(path).name,
            metadata=dict(metadata or {}),
        )


@dataclass
class OutboundMessage:
    """平台无关出站消息。"""

    conversation: ConversationKey
    segments: List[OutboundSegment]
    reason: str = "reply"

    @classmethod
    def text(
        cls,
        conversation: ConversationKey,
        text: str,
        *,
        reason: str = "reply",
        kind: str = "text",
    ) -> "OutboundMessage":
        return cls(
            conversation=conversation,
            segments=[OutboundSegment.text_segment(text, kind=kind)],
            reason=reason,
        )
