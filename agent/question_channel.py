"""向用户问询的统一通道。

目的：把 ask_user_question_tool 的"emit 事件 + 阻塞等答"的协作模式抽出来，
让其他工具（典型的是 bash_permission 的权限弹框）也能复用同一条 UI 路径，
不再在工具内自己 print + input()——那条路在 TUI 模式下 stdin 被前端接管，
会失败成 permission_unavailable。

给消费方两种调用方式：
- ask(question, options, recommended_index?) → {"answer": label, "cancelled": bool, ...}
- 取消信号沿用 cancel_token；上游中断（Ctrl+C）能立即让等待线程退出

设计上故意只依赖 event_bus + question_registry，不依赖 Tool/Session，方便单测。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from agent.cancel import get_current_cancel_token
from agent.event_bus import EventBus
from agent.events import AskUserQuestion, AskUserQuestionAnswered
from agent.question_registry import QuestionRegistry


class QuestionChannel:
    def __init__(self, registry: QuestionRegistry, bus: EventBus) -> None:
        self._registry = registry
        self._bus = bus

    def ask(
        self,
        question: str,
        options: List[Dict[str, str]],
        multi_select: bool = False,
        recommended_index: Optional[int] = None,
    ) -> Dict[str, Any]:
        """同步发问、阻塞等答。返回 {"answer": str, "answers": list, "other_text": str?,
        "cancelled": bool}。
        """
        qid = self._registry.new_question_id()
        self._registry.register(qid)

        self._bus.emit(AskUserQuestion(
            question_id=qid,
            question=question,
            options=options,
            multi_select=multi_select,
            recommended_index=recommended_index,
            allow_other=True,
        ))

        cancel_token = get_current_cancel_token()
        cancel_event = cancel_token.event if cancel_token is not None else None

        try:
            slot = self._registry.wait_for_answer(qid, cancel_event=cancel_event)
        finally:
            self._registry.discard(qid)

        self._bus.emit(AskUserQuestionAnswered(
            question_id=qid,
            selected_labels=list(slot.selected_labels),
            other_text=slot.other_text,
            cancelled=slot.cancelled,
        ))

        if slot.cancelled:
            return {"cancelled": True}

        out: Dict[str, Any] = {"cancelled": False}
        if multi_select:
            out["answers"] = list(slot.selected_labels)
        else:
            out["answer"] = slot.selected_labels[0] if slot.selected_labels else ""
        if slot.other_text:
            out["other_text"] = slot.other_text
        return out
