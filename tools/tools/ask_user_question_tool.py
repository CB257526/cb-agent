"""AskUserQuestionTool

向用户发起多选/单选问题，阻塞工具线程等待 UI 端回答。

参考 Claude Code AskUserQuestionTool（外部代码/13-AskUserQuestionTool）做了简化：
- 单条问题（参考是 1-4 题；这里一次问一题，多题让模型多次调）
- 2-4 个选项 + 可选 multi_select
- recommended_index：第几个是推荐项；UI 决定怎么标记
- "Other" 兜底由 UI 永远显示，让用户能填自定义文本
- 不带 preview / annotations（参考的高级特性，初版不做）

返回给模型的 JSON：
- 单选: {"question":"...", "answer":"Label A"}
- 多选: {"question":"...", "answers":["A","B"]}
- Other: {"question":"...", "answer":"Other", "other_text":"用户填的字"}
- 取消: {"cancelled": true, "reason": "..."}
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agent.cancel import get_current_cancel_token
from agent.event_bus import EventBus
from agent.events import AskUserQuestion, AskUserQuestionAnswered
from agent.question_registry import QuestionRegistry
from tools.tool import Tool, ToolParameter


class AskUserQuestionTool(Tool):
    def __init__(self, question_registry: QuestionRegistry, event_bus: EventBus) -> None:
        super().__init__(
            name="ask_user_question",
            description=(
                "向用户发起一道多选/单选问题，等用户在 UI 中选择后再继续。"
                "适用：方案选择、操作确认、信息补充、偏好询问、流程分支。"
                "选项 2-4 个互斥；UI 会自动追加 'Other' 让用户填自定义文本。"
                "如有推荐项放第一个并设 recommended_index=0。"
                "不要用本工具询问'是否继续/计划是否 OK'——直接做或交给更明确的工具。"
            ),
        )
        self._registry = question_registry
        self._bus = event_bus

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="question", type="string", required=True,
                description="要问的问题，简洁明了，以问号结尾。",
            ),
            ToolParameter(
                name="options", type="array", required=True,
                description="2-4 个互斥选项，每项 {label, description}。不要自己加 'Other'。",
                items={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["label", "description"],
                },
            ),
            ToolParameter(
                name="multi_select", type="boolean", required=False, default=False,
                description="允许多选则 true。默认 false。",
            ),
            ToolParameter(
                name="recommended_index", type="number", required=False,
                description="推荐项的 0-based 索引；无推荐不传。",
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        q = parameters.get("question")
        opts = parameters.get("options")
        if not isinstance(q, str) or not q.strip():
            return False
        if not isinstance(opts, list) or not (2 <= len(opts) <= 4):
            return False
        labels: List[str] = []
        for opt in opts:
            if not isinstance(opt, dict):
                return False
            label = opt.get("label")
            if not isinstance(label, str) or not label.strip():
                return False
            if not isinstance(opt.get("description", ""), str):
                return False
            labels.append(label)
        if len(set(labels)) != len(labels):
            return False
        rec = parameters.get("recommended_index")
        if rec is not None:
            if not isinstance(rec, int) or isinstance(rec, bool):
                return False
            if not (0 <= rec < len(opts)):
                return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps(
                {"error": "参数无效：检查 question/options/recommended_index"},
                ensure_ascii=False,
            )

        question: str = parameters["question"]
        options: List[Dict[str, str]] = [
            {"label": str(o["label"]), "description": str(o.get("description", ""))}
            for o in parameters["options"]
        ]
        multi_select: bool = bool(parameters.get("multi_select", False))
        rec = parameters.get("recommended_index")
        recommended_index: Optional[int] = rec if isinstance(rec, int) and not isinstance(rec, bool) else None

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
            return json.dumps(
                {"cancelled": True, "reason": "user cancelled or interrupted"},
                ensure_ascii=False,
            )

        if multi_select:
            payload: Dict[str, Any] = {
                "question": question,
                "answers": list(slot.selected_labels),
            }
            if slot.other_text:
                payload["other_text"] = slot.other_text
            return json.dumps(payload, ensure_ascii=False)

        # 单选
        answer = slot.selected_labels[0] if slot.selected_labels else ""
        payload = {"question": question, "answer": answer}
        if slot.other_text:
            payload["other_text"] = slot.other_text
        return json.dumps(payload, ensure_ascii=False)
