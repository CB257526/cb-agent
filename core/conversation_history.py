"""模型可见对话的唯一内存历史。

这个模块不负责拼提示词，也不理解具体 provider。它只维护已经确定要让模型看到的
消息序列。普通运行只能追加；正式 compact 才能替换整代历史。
"""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Iterator, Sequence
from typing import Any, Optional

from core.message import Message


def _stable_protocol_digest(message: Message) -> str:
    """计算 provider 协议字段的稳定摘要，检测旧消息被意外改写。"""

    encoded = json.dumps(
        message.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def freeze_history_message(
    message: Message,
    *,
    turn_id: str = "",
    item_id: str = "",
) -> Message:
    """复制消息并补齐不可变身份字段。

    Pydantic ``Message`` 仍服务旧工具与 UI 接口，因此这里不直接把整个类改成 frozen。
    所有进入唯一历史的消息都先深拷贝，之后用 digest 在请求边界验证没有被改写。
    """

    frozen = message.model_copy(deep=True)
    metadata = dict(frozen.metadata or {})
    metadata.setdefault("item_id", item_id or uuid.uuid4().hex)
    if turn_id:
        metadata.setdefault("turn_id", str(turn_id))
    frozen.metadata = metadata
    metadata["content_digest"] = _stable_protocol_digest(frozen)
    return frozen


class ConversationHistory(Sequence[Message]):
    """单会话唯一的模型可见历史。

    ``generation`` 只在 compact/迁移等明确边界变化。正常用户回合和工具循环只通过
    ``append_batch`` 增长，绝不在请求前后反向提取或改写旧消息。
    """

    def __init__(
        self,
        items: Sequence[Message] = (),
        *,
        generation: int = 0,
    ) -> None:
        self._items = [message.model_copy(deep=True) for message in items]
        self.generation = max(0, int(generation))
        self._verify_all()

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __iter__(self) -> Iterator[Message]:
        return iter(self._items)

    def __eq__(self, other: object) -> bool:
        """按消息序列比较，兼容 UI/测试对空列表的只读判断。"""

        if isinstance(other, ConversationHistory):
            return self._items == other._items
        if isinstance(other, Sequence):
            return self._items == list(other)
        return False

    def snapshot(self) -> tuple[Message, ...]:
        """返回当前代的深拷贝快照，供 compact、估算和审计使用。"""

        self._verify_all()
        return tuple(message.model_copy(deep=True) for message in self._items)

    def logical_messages(self) -> list[dict[str, Any]]:
        """返回不展开 ImageRef 的逻辑消息，供 journal 校验和本地诊断。"""

        self._verify_all()
        return [copy.deepcopy(message.to_dict()) for message in self._items]

    def provider_messages(self, media_store: Any = None) -> list[dict[str, Any]]:
        """从唯一逻辑历史生成一次性 provider payload。

        无图片的旧调用可省略 ``media_store``；一旦 history 含 ImageRef，缺少存储
        会显式失败，禁止把项目内部内容块误发给 OpenAI-compatible provider。
        """

        self._verify_all()
        return [
            copy.deepcopy(message.to_provider_dict(media_store))
            for message in self._items
        ]

    def prepare_batch(
        self,
        messages: Sequence[Message],
        *,
        turn_id: str = "",
    ) -> list[Message]:
        """冻结一批待追加消息；调用方应先持久化，再调用 ``append_prepared``。"""

        # 必须在 journal 写入新事件之前发现旧消息污染，否则磁盘会比当前内存多出
        # 一条本不应提交的后续事件，增加恢复时的歧义。
        self._verify_all()
        return [freeze_history_message(message, turn_id=turn_id) for message in messages]

    def append_prepared(self, messages: Sequence[Message]) -> None:
        """追加已经成功写入 journal 的消息。"""

        self._verify_all()
        prepared = [message.model_copy(deep=True) for message in messages]
        for message in prepared:
            self._verify_message(message)
        self._items.extend(prepared)

    def append_batch(
        self,
        messages: Sequence[Message],
        *,
        turn_id: str = "",
    ) -> list[Message]:
        """无持久化会话使用的直接追加入口。"""

        prepared = self.prepare_batch(messages, turn_id=turn_id)
        self.append_prepared(prepared)
        return prepared

    def replace_prepared(
        self,
        messages: Sequence[Message],
        *,
        generation: int,
    ) -> None:
        """安装已经成功写入 journal 的正式 replacement history。"""

        next_generation = int(generation)
        if next_generation <= self.generation:
            raise ValueError("replacement generation 必须严格递增")
        replacement = [message.model_copy(deep=True) for message in messages]
        for message in replacement:
            self._verify_message(message)
        self._items = replacement
        self.generation = next_generation

    def clear_memory(self) -> None:
        """仅供会话 clear/switch 使用；普通运行禁止调用。"""

        self._items = []
        self.generation = 0

    @staticmethod
    def kind(message: Message) -> str:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        return str(metadata.get("kind") or "")

    @staticmethod
    def turn_id(message: Message) -> str:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        return str(metadata.get("turn_id") or "")

    def last_turn_id(self) -> str:
        for message in reversed(self._items):
            turn_id = self.turn_id(message)
            if turn_id:
                return turn_id
        return ""

    def _verify_all(self) -> None:
        for message in self._items:
            self._verify_message(message)

    @staticmethod
    def _verify_message(message: Message) -> None:
        metadata = message.metadata if isinstance(message.metadata, dict) else {}
        expected: Optional[str] = metadata.get("content_digest")
        if expected and str(expected) != _stable_protocol_digest(message):
            raise RuntimeError(
                f"canonical history 消息被改写: item_id={metadata.get('item_id', '')}"
            )


__all__ = ["ConversationHistory", "freeze_history_message"]
