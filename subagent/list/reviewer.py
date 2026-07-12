"""代码审查型子代理。"""

from subagent.models import SubagentDefinition, SubagentPermissionPolicy


REVIEWER_SUBAGENT = SubagentDefinition(
    name="reviewer",
    description="只读代码审查代理，重点发现缺陷、回归、权限风险和测试缺口。",
    system_prompt=(
        "你是 cb-agent 的代码审查子代理。以缺陷和风险为先，检查行为回归、并发、持久化、权限边界"
        "和测试覆盖。你只能读取与执行只读检查，不能修改代码。最终按严重程度列出问题，每项给出"
        "文件位置、触发条件、影响和建议修复方向；没有问题时明确说明残余风险。不得调用其他子代理。"
    ),
    tools=("bash", "file_read", "glob", "grep", "list_tools", "ls"),
    max_turns=28,
    permissions=SubagentPermissionPolicy(bash_mode="read_only"),
    source_path=__file__,
    builtin=True,
)


__all__ = ["REVIEWER_SUBAGENT"]
