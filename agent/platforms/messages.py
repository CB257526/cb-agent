"""通讯平台统一消息结构。

设计目标是让 QQ/NapCat、未来微信等平台复用同一套 Agent 入口与事件出口。
平台适配器负责协议细节，Agent 和工具只看到这些平台无关的数据类。

核心数据流：
  入站：OneBot JSON → InboundMessage → prompt_text() → LLM
  出站：LLM → EventBus → PlatformEventRenderer → OutboundMessage → OneBot 消息段

这层的每一个类都刻意不引入任何 QQ/微信的协议概念（CQ 码、消息段类型等），
保证新增平台时只需写一个新的 adapter，不影响 Agent 核心逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ═══════════════════════════════════════════════════════════════════════════
# ConversationKey —— 会话标识
# ═══════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)  # frozen=True：不可变，可安全用作 dict 的 key 和 ContextVar 的值
class ConversationKey:
    """唯一标识一个通讯软件会话。

    ``platform`` 用来区分 qq/wechat 等平台，
    ``kind`` 用来区分 private/group，
    ``id`` 是平台侧会话 ID（群号或用户 QQ 号）。

    后续做多平台并发时，这个结构可以直接作为路由键。
    """

    platform: str  # 平台标识："qq" / "wechat" / "telegram"
    kind: str      # 会话类型："private"（私聊）或 "group"（群聊）
    id: str        # 平台会话 ID：私聊时为对方 QQ 号，群聊时为群号

    @property
    def stable_id(self) -> str:
        """生成稳定、可打印的唯一会话标识。

        三个字段用冒号拼接，例如 "qq:private:123456" 或 "qq:group:789012"。
        用途：作为字典 key 做会话路由、日志中标识会话。
        """
        return f"{self.platform}:{self.kind}:{self.id}"

    def to_dict(self) -> Dict[str, str]:
        """序列化为普通字典，方便 JSON 日志或跨进程传递。"""
        return {"platform": self.platform, "kind": self.kind, "id": self.id}


# ═══════════════════════════════════════════════════════════════════════════
# InboundAttachment —— 入站附件
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InboundAttachment:
    """通讯软件收到的附件。

    一个附件可能只包含 URL（图片/文件还没下载到本地），也可能已经下载完成有本地路径。
    平台适配器的职责是尽量下载到本地，失败时保留 URL 描述让模型至少知道"用户发了附件"。

    字段说明：
      modality:  附件类型 —— "image" / "audio" / "video" / "file"
      path:      已保存到本地的文件绝对路径；None 表示尚未下载或下载失败
      url:       平台提供的原始 URL；适配器下载时用，下载失败后 fallback 到 description
      file_id:   平台文件 ID（NapCat 用来调用 get_file_url action）；path 外的重要补全凭据
      file_name: 原始文件名，用于日志和 LLM 提示
      description: 人类可读的附件说明；path 为空时模型通过它感知附件存在
      metadata:  平台原始元数据，透明保存不做修改
    """

    modality: str                        # 附件类型
    path: Optional[str] = None           # 本地文件路径（已下载）
    url: Optional[str] = None            # 平台原始 URL
    file_id: Optional[str] = None        # 平台文件 ID
    file_name: str = ""                  # 文件名
    description: str = ""                # 人类可读描述
    metadata: Dict[str, Any] = field(default_factory=dict)  # 平台原始元数据

    def to_prompt_attachment(self) -> Optional[Dict[str, Any]]:
        """转为多模态输入层接受的附件格式。

        只有已下载到本地的附件（path 非空）才返回有效值，
        否则返回 None，表示这个附件无法作为多模态输入。

        返回格式:
          {"path": "/abs/path/to/img.jpg", "modality": "image", "source": "direct"}
        """
        if not self.path:
            return None
        return {
            "path": self.path,
            "modality": self.modality,
            "source": "direct",  # 标记来源为"用户直接发送"，区别于工具返回的图片
        }


# ═══════════════════════════════════════════════════════════════════════════
# InboundMessage —— 入站消息（平台 → Agent）
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class InboundMessage:
    """平台入站消息，供 Agent 会话入口消费。

    这是所有通讯平台的统一入站模型。平台适配器把各自的协议（OneBot、微信 OC 等）
    转换到这个结构后，Agent 会话逻辑就不需要知道底层平台细节。

    字段说明：
      conversation:        会话标识（哪个平台、哪个群/人）
      sender_id:           发送者 ID（QQ 号、微信号等）
      sender_name:         发送者显示名（群名片 > 昵称 > ID）
      text:                纯文本消息内容（已清理 CQ 码和唤醒前缀）
      raw:                 平台原始事件，保留供 adapter 后续补全（如查引用消息）
      attachments:         附件列表（图片/文件/语音等）
      message_id:          平台消息 ID，用于去重和引用追踪
      reply_to_message_id: 用户引用回复的那条消息的 ID
      transient_context:   仅本轮有效的上下文（如群聊最近消息摘要），不写入持久化历史
    """

    conversation: ConversationKey                          # 会话标识
    sender_id: str                                         # 发送者 ID
    sender_name: str                                       # 发送者显示名
    text: str                                              # 纯文本消息
    raw: Dict[str, Any] = field(default_factory=dict)      # 平台原始事件
    attachments: List[InboundAttachment] = field(default_factory=list)  # 附件列表
    message_id: Optional[str] = None                       # 平台消息 ID
    reply_to_message_id: Optional[str] = None              # 引用回复的消息 ID
    transient_context: str = ""                            # 仅本轮有效的背景上下文

    def prompt_text(self) -> str:
        """生成传给 Agent 的用户文本（当前轮 prompt）。

        包含：
          1. 平台元信息头 —— 告诉模型消息来自哪个平台/会话/发送者
          2. transient_context —— 仅本轮有效的背景（如群聊最近消息），不落地
          3. 用户原话
          4. 引用标记 —— 如果用户回复了某条消息
          5. 未下载附件的提示 —— 让模型知道"用户发了附件但我看不到内容"

        注意：transient_context 不写入 persistent_text，这样群聊噪声不会污染
        私聊/群聊的长期记忆。
        """

        # 平台元信息头：[通讯软件消息 platform=qq conversation=private:123 sender_id=456 sender=张三]
        header = (
            f"[通讯软件消息 platform={self.conversation.platform} "
            f"conversation={self.conversation.kind}:{self.conversation.id} "
            f"sender_id={self.sender_id} "
            f"sender={self.sender_name or self.sender_id}]"
        )
        parts = [header]

        # transient_context 是临时背景（群聊最近 N 条消息摘要），只服务当前轮推理
        if self.transient_context.strip():
            parts.append(self.transient_context.strip())

        # 用户文本主体
        parts.append(self.text.strip())

        # 标记引用消息 ID
        if self.reply_to_message_id:
            parts.append(f"[引用消息] message_id={self.reply_to_message_id}")

        # 提示模型有附件但未能下载（已下载的附件走多模态输入层，不在这里提示）
        for item in self.attachments:
            if item.path:
                continue  # 已下载附件不在此提示，由多模态层处理
            desc = item.description or item.url
            if desc:
                parts.append(f"[附件提示] {item.modality}: {desc}")

        # 拼接所有段落，去掉空白行
        return "\n".join(p for p in parts if p).strip()

    def persistent_text(self) -> str:
        """生成适合长期保存的用户文本。

        与 prompt_text() 的区别：
          - 不含平台头（"通讯软件消息 platform=..."），那只是当前轮提示
          - 不含 transient_context（群聊背景不应固化进长期记忆）
          - 保留用户原话 + 引用 ID + 未下载附件提示
          - 已下载附件由多模态层追加安全摘要，这里不重复路径和元信息

        这个文本写入 conversation history，用于后续对话时的上下文恢复。
        """

        parts = [self.text.strip()]

        if self.reply_to_message_id:
            parts.append(f"[引用消息] message_id={self.reply_to_message_id}")

        for item in self.attachments:
            if item.path:
                continue  # 已下载附件多模态层会处理
            desc = item.description or item.url
            if desc:
                parts.append(f"[附件提示] {item.modality}: {desc}")

        return "\n".join(p for p in parts if p).strip()

    def prompt_attachments(self) -> List[Dict[str, Any]]:
        """提取已下载附件列表，供多模态输入层使用。

        只返回有本地路径的附件（path 非空），每个附件包装为
        {"path": ..., "modality": ..., "source": "direct"} 格式。
        """

        return [
            payload
            for item in self.attachments                # 遍历所有附件
            for payload in [item.to_prompt_attachment()] # 每个附件最多产生一个有效 payload
            if payload is not None                       # 过滤掉未下载的附件
        ]


# ═══════════════════════════════════════════════════════════════════════════
# OutboundSegment —— 出站消息段
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OutboundSegment:
    """平台无关出站消息段。

    一条 OutboundMessage 可以包含多个 OutboundSegment，
    例如先发一段文字说明，再发一张图片。

    ``kind`` 支持 text/image/audio/video/file/sticker/question/todo/status。
    平台不支持某种段时，应由适配器降级成 text/status，而不是让 Agent 主流程报错。

    字段说明：
      kind:      消息段类型
      text:      文本内容（text/todo/question/status 类使用）
      path:      资源文件本地路径（image/audio/video/file 类使用）
      file_name: 文件名（发文件时显示）
      mime_type: MIME 类型，可选
      metadata:  附加元信息（如文件交付方式、来源等）
    """

    kind: str                                  # 消息段类型
    text: str = ""                             # 文本内容
    path: str = ""                             # 文件路径
    file_name: str = ""                        # 文件名
    mime_type: str = ""                        # MIME 类型
    metadata: Dict[str, Any] = field(default_factory=dict)  # 附加元信息

    @classmethod
    def text_segment(cls, text: str, *, kind: str = "text") -> "OutboundSegment":
        """快捷构造纯文本消息段。

        kind 可以是 "text"（普通消息）、"status"（状态提示）、"question"（询问选项）等。
        """
        return cls(kind=kind, text=text)

    @classmethod
    def file_segment(
        cls,
        *,
        kind: str,                             # 文件类型：image / audio / video / file
        path: str,                             # 本地文件绝对路径
        file_name: str = "",                   # 显示文件名，为空则从 path 提取
        text: str = "",                        # 可选的附带文本（如图片说明）
        metadata: Optional[Dict[str, Any]] = None,  # 附加元信息
    ) -> "OutboundSegment":
        """快捷构造资源文件消息段。

        path 会被转为平台无关格式（Path 规范化），
        file_name 未指定时自动从 path 提取文件名。
        """
        return cls(
            kind=kind,
            text=text,
            path=str(Path(path)),              # 规范化路径（正斜杠、解析相对路径）
            file_name=file_name or Path(path).name,  # 自动提取文件名
            metadata=dict(metadata or {}),     # 复制一份避免外部修改
        )


# ═══════════════════════════════════════════════════════════════════════════
# OutboundMessage —— 出站消息（Agent → 平台）
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class OutboundMessage:
    """平台无关出站消息。

    由 PlatformEventRenderer 或 QQ tool 构造，平台适配器负责翻译成具体协议。

    字段说明：
      conversation: 目标会话
      segments:     消息段列表（可以混合 text + image + file）
      reason:       发送原因，用于日志追踪和降级策略：
                      "reply" —— 正常回复
                      "done" —— Agent 完成后的最终答案
                      "error" —— 异常提示
                      "tool_start" / "tool_permission_denied" 等
    """

    conversation: ConversationKey            # 目标会话
    segments: List[OutboundSegment]          # 消息段列表
    reason: str = "reply"                    # 发送原因（日志/追踪用）

    @classmethod
    def text(
        cls,
        conversation: ConversationKey,       # 目标会话
        text: str,                           # 文本内容
        *,
        reason: str = "reply",               # 发送原因
        kind: str = "text",                  # 文本类型（text/status/todo/question）
    ) -> "OutboundMessage":
        """快捷构造纯文本出站消息。"""
        return cls(
            conversation=conversation,
            segments=[OutboundSegment.text_segment(text, kind=kind)],
            reason=reason,
        )
