"""模型可见运行现场的持久化快照与增量比较。

DynamicSectionResult 提供读取三元组：present（成功且存在）/ absent（确认不存在）/
error（读取失败）。读取失败应保留 baseline 旧值，不能生成 removed 更新。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Sequence


@dataclass(frozen=True)
class DynamicSectionResult:
    """动态 section 的完整读取结果。

    status:
      - present: 读取成功且内容存在
      - absent:  读取成功但确认不存在（如可选 section 无内容）
      - error:   读取失败，不会生成 removed 更新

    scope:
      - world_state:  当前仍然成立的环境状态，参与 baseline diff
      - turn_evidence: 某个回合实际提供给模型的证据，不参与 baseline，但同样进入 history
    """

    name: str
    status: Literal["present", "absent", "error"] = "present"
    text: str = ""
    error: str = ""
    scope: Literal["world_state", "turn_evidence"] = "world_state"

    @classmethod
    def present(
        cls,
        name: str,
        text: str,
        *,
        scope: Literal["world_state", "turn_evidence"] = "world_state",
    ) -> "DynamicSectionResult":
        return cls(name=name, status="present", text=str(text or "").strip(), scope=scope)

    @classmethod
    def absent(
        cls,
        name: str,
        *,
        scope: Literal["world_state", "turn_evidence"] = "world_state",
    ) -> "DynamicSectionResult":
        return cls(name=name, status="absent", scope=scope)

    @classmethod
    def error_result(
        cls,
        name: str,
        error: str = "",
        *,
        scope: Literal["world_state", "turn_evidence"] = "world_state",
    ) -> "DynamicSectionResult":
        return cls(
            name=name,
            status="error",
            error=str(error or ""),
            scope=scope,
        )


@dataclass(frozen=True)
class WorldStateDiff:
    """当前现场相对上一份基线的变化。"""

    changed: list[tuple[str, str]]
    removed: list[str]


@dataclass(frozen=True)
class WorldStateSnapshot:
    """按稳定 section 名保存模型已经看过的规范现场值。"""

    sections: dict[str, str]

    @classmethod
    def from_sections(cls, sections: Sequence[tuple[str, str]]) -> "WorldStateSnapshot":
        """归一化具名 section，同名 section 以最后一个值为准。"""

        normalized: dict[str, str] = {}
        for name, text in sections:
            key = str(name or "").strip()
            value = str(text or "").strip()
            if key and value:
                normalized[key] = value
        return cls(sections=normalized)

    @classmethod
    def from_payload(cls, payload: object) -> "WorldStateSnapshot":
        """从持久化 JSON 恢复快照，异常字段按空快照处理。"""

        if not isinstance(payload, Mapping):
            return cls(sections={})
        return cls(
            sections={
                str(name): str(value)
                for name, value in payload.items()
                if name and isinstance(value, str) and value.strip()
            }
        )

    def to_payload(self) -> dict[str, str]:
        """返回可直接写入 JSON 的稳定字典。"""

        return dict(self.sections)

    def diff(self, previous: "WorldStateSnapshot") -> WorldStateDiff:
        """计算新增、变化和删除的 section。"""

        changed = [
            (name, text)
            for name, text in self.sections.items()
            if previous.sections.get(name) != text
        ]
        removed = sorted(set(previous.sections) - set(self.sections))
        return WorldStateDiff(changed=changed, removed=removed)


EMPTY_WORLD_STATE = WorldStateSnapshot(sections={})


__all__ = [
    "DynamicSectionResult",
    "EMPTY_WORLD_STATE",
    "WorldStateDiff",
    "WorldStateSnapshot",
]
