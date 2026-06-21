"""流式解析 ``<proposed_plan>`` 块的增量解析器。

Plan Mode 下，LLM 在流式输出时可能随时插入 <proposed_plan>...</proposed_plan> 块。
这个解析器从逐 chunk 到达的文本流中实时切分出两种片段：
- normal: 计划块外的普通回答文本（继续走 TextDelta 渲染）
- plan_delta: 计划块内的 Markdown 文本（走 PlanDelta 渲染到独立计划面板）

设计要点：
- 标签可能被 LLM 分片输出（如 "<pro" + "posed_plan>"），解析器用 _buffer + 前缀
  匹配来处理跨 chunk 的标签边界。
- _prefix_suffix_len() 计算 buffer 尾部与标签前缀的重叠长度，保留重叠部分等待
  下一个 chunk 拼接，避免把半个标签误当普通文本输出。
- 同一轮可能输出多个 <proposed_plan> 块，只有最后一个块的内容会被保存为 pending plan。
- split_proposed_plan_text() 用于非流式场景（回答已完成），从完整文本中提取可见部分
  和最后一个计划块。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple


OPEN_TAG = "<proposed_plan>"
CLOSE_TAG = "</proposed_plan>"


@dataclass
class PlanSegment:
    kind: str
    text: str = ""


def _prefix_suffix_len(text: str, marker: str) -> int:
    max_len = min(len(text), len(marker) - 1)
    for size in range(max_len, 0, -1):
        if marker.startswith(text[-size:]):
            return size
    return 0


class ProposedPlanParser:
    """将流式文本拆分为 normal 文本段和 proposed-plan 段。

    用法: 每次 LLM 输出 chunk 时调用 push(chunk)，最后调用 finish()。
    返回的 PlanSegment 列表按到达顺序排列，kind 取值:
    - "normal": 计划块外的普通回答文本 → 走 TextDelta
    - "plan_start": 检测到 <proposed_plan> 开始标签
    - "plan_delta": 计划块内的增量文本 → 走 PlanDelta
    - "plan_end": 检测到 </proposed_plan> 结束标签

    内部用 _buffer 处理跨 chunk 的标签边界。
    """

    def __init__(self) -> None:
        self._buffer = ""
        self._in_plan = False

    def push(self, chunk: str) -> List[PlanSegment]:
        if not chunk:
            return []
        self._buffer += chunk
        return self._drain(final=False)

    def finish(self) -> List[PlanSegment]:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> List[PlanSegment]:
        out: List[PlanSegment] = []
        while self._buffer:
            marker = CLOSE_TAG if self._in_plan else OPEN_TAG
            idx = self._buffer.find(marker)
            if idx >= 0:
                before = self._buffer[:idx]
                if before:
                    out.append(PlanSegment("plan_delta" if self._in_plan else "normal", before))
                self._buffer = self._buffer[idx + len(marker):]
                if self._in_plan:
                    out.append(PlanSegment("plan_end"))
                    self._in_plan = False
                else:
                    out.append(PlanSegment("plan_start"))
                    self._in_plan = True
                continue

            keep = 0 if final else _prefix_suffix_len(self._buffer, marker)
            emit_text = self._buffer[: len(self._buffer) - keep] if keep else self._buffer
            self._buffer = self._buffer[len(emit_text):]
            if emit_text:
                out.append(PlanSegment("plan_delta" if self._in_plan else "normal", emit_text))
            break

        if final and self._in_plan:
            out.append(PlanSegment("plan_end"))
            self._in_plan = False
        return out


def split_proposed_plan_text(text: str) -> Tuple[str, Optional[str]]:
    """从完整文本中分离可见文本和最后一个 proposed plan 块。

    用于非流式场景（如 LLM 回答已完成，不走 push/finish 流式路径）:
    - 返回 (visible_text, last_plan | None)
    - 如果有多个 <proposed_plan> 块，只返回最后一个（与流式行为一致）
    - visible_text 是所有非计划块文本的拼接（块标签本身被移除）
    """

    parser = ProposedPlanParser()
    visible: List[str] = []
    current_plan: List[str] = []
    last_plan: Optional[str] = None
    for segment in parser.push(text) + parser.finish():
        if segment.kind == "normal":
            visible.append(segment.text)
        elif segment.kind == "plan_start":
            current_plan = []
        elif segment.kind == "plan_delta":
            current_plan.append(segment.text)
        elif segment.kind == "plan_end":
            last_plan = "".join(current_plan)
            current_plan = []
    return "".join(visible), last_plan


__all__ = [
    "CLOSE_TAG",
    "OPEN_TAG",
    "PlanSegment",
    "ProposedPlanParser",
    "split_proposed_plan_text",
]
