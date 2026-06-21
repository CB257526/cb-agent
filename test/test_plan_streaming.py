"""Plan Mode 流式解析集成测试。

验证 _PlanParsingEventBus 与 AgentSession 的集成行为：
- 流式 chunk 中的多计划块处理
- 最后一个计划块被保存（前面的被覆盖）
- PlanReady 事件正确 emit
"""

from agent.event_bus import EventBus, collect_all
from agent.events import PlanReady, PlanStart, TextDelta
from agent.session import _PlanParsingEventBus


class _FakeSession:
    """模拟 AgentSession 的最小桩。

    只暴露 Plan Mode 集成所需的两个接口:
    - event_bus: 用于 emit 事件
    - _save_pending_plan: 记录保存的计划（验证去重逻辑）
    """

    def __init__(self):
        self.event_bus = EventBus()
        self.saved_plans = []

    def _save_pending_plan(self, plan: str, *, round_idx: int = 0):
        """模拟持久化。保存计划文本和 round_idx 到列表供断言检查。"""
        self.saved_plans.append((plan, round_idx))
        state = {"mode": "plan", "status": "pending", "revision": len(self.saved_plans)}
        self.event_bus.emit(PlanReady(plan=plan, plan_state=state, round_idx=round_idx))
        return state


def test_streaming_plan_bus_saves_only_last_plan_block():
    """验证流式解析只保存最后一个 <proposed_plan> 块。

    场景：
    - emit 两个包含 <proposed_plan> 的 TextDelta chunk
    - 第一个块 "first" 应在 finish() 时被第二个块 "second" 覆盖
    - saved_plans 只有一条记录（"second"）
    - PlanStart 事件被 emit 2 次（两个块的开始标签各一次）
    - PlanReady 事件只被 emit 1 次（只有最后一个块被保存）
    """
    session = _FakeSession()
    events = collect_all(session.event_bus)
    bus = _PlanParsingEventBus(session)

    # 第一个 chunk 包含完整的计划块 "first"
    bus.emit(TextDelta(delta="intro<proposed_plan>first</proposed_plan>middle", round_idx=4))
    # 第二个 chunk 包含完整的计划块 "second"（覆盖前一个）
    bus.emit(TextDelta(delta="<proposed_plan>second</proposed_plan>outro", round_idx=4))
    bus.finish(round_idx=4)

    # 只有最后一个计划块被保存
    assert session.saved_plans == [("second", 4)]
    assert [e.plan for e in events if isinstance(e, PlanReady)] == ["second"]
    # PlanStart 出现两次（每个块开始标签各一次）
    assert len([e for e in events if isinstance(e, PlanStart)]) == 2
