"""Plan Mode 会话级状态管理单元测试。

覆盖 PlanStateStore 的核心生命周期:
- 会话隔离（不同 session 的 plan 状态互不影响）
- 审批/拒绝流程（状态机转换正确性）
- /clear 命令（只清当前会话的 plan）
- 空会话下的 load 安全性（不创建新 session）
"""

from agent.plan_state import PlanStateStore
from agent.work_context import LocalSessionStore


def test_plan_state_is_isolated_per_session(tmp_path):
    """验证不同 session 的 plan state 完全隔离。

    创建两个 session，各自提交不同计划，切换后验证:
    - session A 看到自己的计划
    - session B 看到自己的计划
    - 切换后 load() 返回的是目标 session 的状态
    """
    session_store = LocalSessionStore(tmp_path / "sessions")
    plan_store = PlanStateStore(session_store=session_store)

    first_id = session_store.active_session_id
    first_state = plan_store.save_pending_plan("# First\n- inspect A")
    assert first_state["pending_revision"] == 1
    assert first_state["pending_plan"] == "# First\n- inspect A"

    # 创建新会话，验证 plan 状态是全新的
    second = session_store.create_session()
    second_id = second["session_id"]
    assert second_id != first_id
    assert plan_store.load()["pending_plan"] == ""

    second_state = plan_store.save_pending_plan("# Second\n- inspect B")
    assert second_state["pending_plan"] == "# Second\n- inspect B"

    # 切回第一个会话，验证 plan 状态完整恢复
    session_store.switch_session(first_id)  # type: ignore[arg-type]
    assert plan_store.load()["pending_plan"] == "# First\n- inspect A"

    session_store.switch_session(second_id)
    assert plan_store.load()["pending_plan"] == "# Second\n- inspect B"


def test_approve_and_reject_update_state_without_losing_revision(tmp_path):
    """验证 approve/reject 状态机转换。

    流程：提交 → 拒绝 → 重新提交 → 批准
    - reject 后 status=rejected, mode=plan, pending_plan 保留
    - approve 后 status=approved, mode=execute, approved_plan 存在
    - approved_revision == pending_revision（批准的是最新版本）
    """
    session_store = LocalSessionStore(tmp_path / "sessions")
    plan_store = PlanStateStore(session_store=session_store)

    plan_store.save_pending_plan("# Plan\n- do it")
    rejected = plan_store.reject("Need safer rollout")
    assert rejected["mode"] == "plan"
    assert rejected["status"] == "rejected"
    assert rejected["last_feedback"] == "Need safer rollout"
    assert rejected["pending_plan"] == "# Plan\n- do it"

    plan_store.save_pending_plan("# Plan v2\n- safer rollout")
    approved = plan_store.approve()
    assert approved["mode"] == "execute"
    assert approved["status"] == "approved"
    assert approved["approved_plan"] == "# Plan v2\n- safer rollout"
    assert approved["approved_revision"] == approved["pending_revision"]


def test_clear_only_removes_active_session_plan(tmp_path):
    """验证 clear() 只清空当前活跃 session 的计划，不影响其他 session。

    创建两个 session 都提交计划，只在第二个 session 上调用 clear()。
    第一个 session 的计划应完整保留。
    """
    session_store = LocalSessionStore(tmp_path / "sessions")
    plan_store = PlanStateStore(session_store=session_store)

    first_id = session_store.active_session_id
    plan_store.save_pending_plan("# First")

    second_id = session_store.create_session()["session_id"]
    plan_store.save_pending_plan("# Second")

    # 清空第二个 session
    cleared = plan_store.clear()
    assert cleared["pending_plan"] == ""
    assert cleared["status"] == "idle"

    # 第一个 session 的计划不受影响
    session_store.switch_session(first_id)  # type: ignore[arg-type]
    assert plan_store.load()["pending_plan"] == "# First"

    session_store.switch_session(second_id)
    assert plan_store.load()["pending_plan"] == ""


def test_load_after_session_clear_does_not_create_new_session(tmp_path):
    """验证 clear 后 load() 不会意外创建新 session。

    调用 clear_active_session() 后 active_session_id 为 None，
    此时 load() 应返回空状态（mode=execute, status=idle），
    而不应调用 ensure_active() 创建新 session 目录。
    """
    session_store = LocalSessionStore(tmp_path / "sessions")
    session_store.clear_active_session()
    assert session_store.active_session_id is None

    state = PlanStateStore(session_store=session_store).load()

    assert state["mode"] == "execute"
    assert state["status"] == "idle"
    assert session_store.active_session_id is None
