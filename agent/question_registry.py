"""用户问答阻塞同步器

AskUserQuestionTool 跑在工具线程内，需要"发问 → 阻塞 → 等 UI 回答 → 拿到答案返回"。
跨线程协调点放在这里：
- 工具方 register(question_id) 拿到一个 Pending 句柄
- 工具方 wait_for_answer(qid, cancel_token) 阻塞到答案到达 / token 取消 / 超时
- gateway 收到 RPC session.answer_question 时调 submit_answer(qid, ...) 唤醒工具

并发：threading.Event + Lock 保护 dict。
取消：cancel_token.event 也参与 wait，被 cancel 时立刻返回 cancelled=True。
超时：默认无超时——问题挂着等用户慢慢看是合理的；调用方按需传 timeout。
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class PendingAnswer:
    """单条问题的等待槽位。"""
    question_id: str
    event: threading.Event = field(default_factory=threading.Event)
    selected_labels: List[str] = field(default_factory=list)
    other_text: Optional[str] = None
    cancelled: bool = False  # True=用户主动取消（区别于 cancel_token 中断）


class QuestionRegistry:
    """跨线程的"问题→答案"等待表。每个 AgentSession 持有一个实例。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: Dict[str, PendingAnswer] = {}

    def new_question_id(self) -> str:
        return f"q_{uuid.uuid4().hex[:10]}"

    def register(self, question_id: str) -> PendingAnswer:
        """工具线程 emit 事件前调；后续 wait_for_answer 用同一个 question_id。"""
        slot = PendingAnswer(question_id=question_id)
        with self._lock:
            self._pending[question_id] = slot
        return slot

    def submit_answer(
        self,
        question_id: str,
        *,
        selected_labels: List[str],
        other_text: Optional[str] = None,
        cancelled: bool = False,
    ) -> bool:
        """gateway 在 stdin 读线程里调。返回 False 表示该问题不存在（已超时/被丢弃）。"""
        with self._lock:
            slot = self._pending.get(question_id)
        if slot is None:
            return False
        slot.selected_labels = list(selected_labels)
        slot.other_text = other_text
        slot.cancelled = cancelled
        slot.event.set()
        return True

    def wait_for_answer(
        self,
        question_id: str,
        *,
        cancel_event: Optional[threading.Event] = None,
        timeout: Optional[float] = None,
    ) -> PendingAnswer:
        """阻塞等用户回答。返回 PendingAnswer——cancelled=True 表示中断或超时。

        cancel_event 优先级：被 set 时立即返回 cancelled=True，不再等用户。
        """
        with self._lock:
            slot = self._pending.get(question_id)
        if slot is None:
            # 不应该发生（register 后立刻调本方法），兜底返回 cancelled
            return PendingAnswer(question_id=question_id, cancelled=True)

        # 双 Event 等待：自己的 event + cancel_event。轮询 50ms，足够低开销
        # 又能即时响应取消。比起额外一个 wait/notify 的同步原语简单。
        deadline: Optional[float] = None
        if timeout is not None:
            import time
            deadline = time.monotonic() + timeout

        while True:
            if slot.event.wait(timeout=0.05):
                return slot
            if cancel_event is not None and cancel_event.is_set():
                slot.cancelled = True
                slot.event.set()  # 让任何后续 submit 直接 noop
                return slot
            if deadline is not None:
                import time
                if time.monotonic() >= deadline:
                    slot.cancelled = True
                    slot.event.set()
                    return slot

    def discard(self, question_id: str) -> None:
        """工具方完成处理后清理。多次调用幂等。"""
        with self._lock:
            self._pending.pop(question_id, None)


__all__ = ["QuestionRegistry", "PendingAnswer"]
