"""Plan Mode 的会话级状态管理与计划版本持久化。

每个 LocalSessionStore session 目录下维护一个 plan/ 子目录：
- state.json: 当前 plan 模式状态（mode / status / revision 等元信息）
- current.md: 最新提交的 pending plan 完整 Markdown 文本
- approved.md: 已批准的计划（approve 时从 current.md 复制）
- revisions/0001.md ... 000N.md: 所有历史提交的版本（每次 save_pending_plan 增量递增）

状态机：
  execute → (用户切 plan) → plan/idle
  plan/idle → (LLM 提交计划) → plan/pending
  plan/pending → (用户拒绝) → plan/rejected → (LLM 重新提交) → plan/pending
  plan/pending → (用户批准) → execute/approved

PlanStateStore 通过 session_store 绑定当前活动会话，切换会话时自动隔离。
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


VALID_MODES = {"execute", "plan"}
VALID_STATUSES = {"idle", "pending", "approved", "rejected"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: Any, limit: int) -> str:
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 1)].rstrip() + "..."


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


@dataclass
class PlanStateStore:
    """Plan 文件存储在活跃 LocalSessionStore 目录下的 plan/ 子目录。

    不直接持有 session_store 的引用则 fallback 到 .cbagent/plan/。
    所有写入操作通过 _atomic_write_json() 先写 .tmp 再 replace，防止写一半崩溃。
    """

    session_store: Optional[Any] = None

    def _fallback_dir(self) -> Path:
        return Path.cwd() / ".cbagent" / "plan"

    def plan_dir(self) -> Path:
        if self.session_store is None:
            return self._fallback_dir()
        self.session_store.ensure_active()
        return self.session_store.active_dir / "plan"

    def _plan_dir_for_read(self) -> Optional[Path]:
        if self.session_store is None:
            return self._fallback_dir()
        active = getattr(self.session_store, "active_session_id", None)
        if not active:
            return None
        try:
            return self.session_store.active_dir / "plan"
        except Exception:
            return None

    def _state_path(self) -> Path:
        return self.plan_dir() / "state.json"

    def _current_path(self) -> Path:
        return self.plan_dir() / "current.md"

    def _approved_path(self) -> Path:
        return self.plan_dir() / "approved.md"

    def _revisions_dir(self) -> Path:
        return self.plan_dir() / "revisions"

    def _read_path(self, name: str) -> Optional[Path]:
        base = self._plan_dir_for_read()
        if base is None:
            return None
        return base / name

    def _blank_state(self) -> Dict[str, Any]:
        return {
            "plan_id": uuid.uuid4().hex,
            "mode": "execute",
            "status": "idle",
            "revision": 0,
            "pending_revision": None,
            "approved_revision": None,
            "current_path": None,
            "approved_path": None,
            "last_feedback": "",
            "updated_at": _now_iso(),
        }

    def load(self, *, include_content: bool = True) -> Dict[str, Any]:
        path = self._read_path("state.json")
        if path is not None and path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    state = self._blank_state()
            except Exception:
                state = self._blank_state()
        else:
            state = self._blank_state()

        if state.get("mode") not in VALID_MODES:
            state["mode"] = "execute"
        if state.get("status") not in VALID_STATUSES:
            state["status"] = "idle"
        try:
            state["revision"] = int(state.get("revision") or 0)
        except Exception:
            state["revision"] = 0

        if include_content:
            state["pending_plan"] = self._read_text_if_exists(self._read_path("current.md"))
            state["approved_plan"] = self._read_text_if_exists(self._read_path("approved.md"))
            state["pending_plan_preview"] = _clip(state.get("pending_plan") or "", 1200)
            state["approved_plan_preview"] = _clip(state.get("approved_plan") or "", 1200)
        return state

    def save(self, state: Dict[str, Any]) -> Dict[str, Any]:
        state = dict(state)
        state["updated_at"] = _now_iso()
        _atomic_write_json(self._state_path(), state)
        return self.load(include_content=True)

    def set_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in VALID_MODES:
            raise ValueError("mode must be 'execute' or 'plan'")
        state = self.load(include_content=False)
        state["mode"] = mode
        return self.save(state)

    def save_pending_plan(self, text: str) -> Dict[str, Any]:
        """保存一份新的 pending plan，revision 自增。

        保存路径：
        - revisions/{revision:04d}.md: 不可变历史版本
        - current.md: 覆盖为最新提交（供 approve 时复制）
        """
        plan = str(text or "").strip()
        if not plan:
            raise ValueError("plan content is empty")
        state = self.load(include_content=False)
        revision = int(state.get("revision") or 0) + 1
        self._revisions_dir().mkdir(parents=True, exist_ok=True)
        revision_path = self._revisions_dir() / f"{revision:04d}.md"
        revision_path.write_text(plan, encoding="utf-8")
        self._current_path().write_text(plan, encoding="utf-8")
        state.update({
            "status": "pending",
            "revision": revision,
            "pending_revision": revision,
            "current_path": str(self._current_path()),
            "last_feedback": "",
        })
        return self.save(state)

    def approve(self) -> Dict[str, Any]:
        state = self.load(include_content=False)
        current = self._current_path()
        if not current.exists():
            raise ValueError("no pending plan to approve")
        self._approved_path().parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(current, self._approved_path())
        state.update({
            "mode": "execute",
            "status": "approved",
            "approved_revision": state.get("pending_revision") or state.get("revision"),
            "approved_path": str(self._approved_path()),
        })
        return self.save(state)

    def reject(self, feedback: str) -> Dict[str, Any]:
        state = self.load(include_content=False)
        state.update({
            "mode": "plan",
            "status": "rejected",
            "last_feedback": str(feedback or "").strip(),
        })
        return self.save(state)

    def clear(self) -> Dict[str, Any]:
        """Remove the active session's persisted plan state."""
        try:
            target = self._plan_dir_for_read()
            if target is not None:
                shutil.rmtree(target, ignore_errors=True)
        except Exception:
            pass
        return self.load(include_content=True)

    def context_text(self) -> str:
        """生成注入 LLM 上下文的 Plan Mode 状态文本。

        根据当前状态输出不同内容：
        - rejected + feedback → 包含拒绝反馈，提示 LLM 修改计划
        - pending → 包含待审计划摘要
        - approved → 包含已批准计划 + "按此计划实施"指令
        - 空闲状态无计划内容 → 返回空字符串（不注入多余上下文）
        """
        state = self.load(include_content=True)
        mode = state.get("mode") or "execute"
        status = state.get("status") or "idle"
        if (
            mode == "execute"
            and status == "idle"
            and not state.get("pending_plan")
            and not state.get("approved_plan")
            and not state.get("last_feedback")
        ):
            return ""
        parts = [f"[Plan Mode State]\nmode={mode}; status={status}; revision={state.get('revision') or 0}"]
        if status == "rejected" and state.get("last_feedback"):
            parts.append(
                "User rejected the previous plan with feedback:\n"
                + _clip(state.get("last_feedback"), 1600)
                + "\nRevise the plan and submit a complete replacement."
            )
        if status in {"pending", "rejected"} and state.get("pending_plan"):
            parts.append(
                "Pending plan:\n"
                + str(state.get("pending_plan") or "")
            )
        if state.get("approved_plan"):
            parts.append(
                "Approved plan for implementation:\n"
                + str(state.get("approved_plan") or "")
                + "\nFollow this approved plan when implementing unless the user changes direction."
            )
        return "\n\n".join(parts)

    @staticmethod
    def _read_text_if_exists(path: Optional[Path]) -> str:
        try:
            if path is not None and path.exists():
                return path.read_text(encoding="utf-8")
        except Exception:
            return ""
        return ""


__all__ = ["PlanStateStore", "VALID_MODES", "VALID_STATUSES"]
