"""Plan Mode 计划块解析器单元测试。

覆盖 ProposedPlanParser 和 split_proposed_plan_text 的核心路径:
- 流式分片标签（跨 chunk 的 <proposed_plan> 边界检测）
- 完整文本中分离计划块与可见文本
- 多计划块时只取最后一个
"""

from agent.plan_parser import ProposedPlanParser, split_proposed_plan_text


def test_streaming_parser_handles_split_tags():
    """验证流式解析器能处理被分片输出的标签。

    LLM 流式输出可能在任何位置断开 chunk，包括标签中间
    （如 "<pro" + "posed_plan>"）。解析器内部通过 _buffer + 前缀匹配
    处理这种边界情况，不应将半个标签误当普通文本输出。
    """
    parser = ProposedPlanParser()
    segments = []
    for chunk in ["hello <pro", "posed_plan># Plan\n", "- read", "\n</proposed", "_plan> bye"]:
        segments.extend(parser.push(chunk))
    segments.extend(parser.finish())

    normal = "".join(s.text for s in segments if s.kind == "normal")
    plan = "".join(s.text for s in segments if s.kind == "plan_delta")
    kinds = [s.kind for s in segments]

    assert normal == "hello  bye"
    assert plan == "# Plan\n- read\n"
    assert "plan_start" in kinds
    assert "plan_end" in kinds


def test_split_proposed_plan_text_separates_visible_text():
    """验证完整文本分离：计划块文本被移除，可见文本保留。

    输入: "intro\n<proposed_plan>...</proposed_plan>\noutro"
    输出: visible="intro\n\noutro", plan="\n# Plan\n- inspect\n"
    """
    visible, plan = split_proposed_plan_text(
        "intro\n<proposed_plan>\n# Plan\n- inspect\n</proposed_plan>\noutro"
    )

    assert visible == "intro\n\noutro"
    assert plan == "\n# Plan\n- inspect\n"


def test_split_proposed_plan_text_uses_last_plan_block():
    """多个 <proposed_plan> 块时只保留最后一个。

    与流式行为一致（_PlanParsingEventBus 的 latest_plan_text 会被后续块覆盖）。
    """
    visible, plan = split_proposed_plan_text(
        "a<proposed_plan>first</proposed_plan>b<proposed_plan>second</proposed_plan>c"
    )

    assert visible == "abc"
    assert plan == "second"
