"""代码库探索型子代理。"""

from subagent.models import SubagentDefinition, SubagentPermissionPolicy


EXPLORE_SUBAGENT = SubagentDefinition(
    name="explore",
    description="快速代码库探索代理，负责定位文件、调用链、约束和现有实现模式。",
    system_prompt=(
        "你是 cb-agent 的代码库探索子代理。你的职责是快速而系统地定位代码、配置、测试和调用链，"
        "不修改任何文件。优先使用 glob、grep、file_read 和只读 Bash，并在结论中给出准确路径、"
        "关键符号和必要的行号。不得调用其他子代理，也不要直接询问用户。"
    ),
    tools=("bash", "file_read", "glob", "grep", "list_tools", "ls"),
    max_turns=24,
    permissions=SubagentPermissionPolicy(bash_mode="read_only"),
    source_path=__file__,
    builtin=True,
)


__all__ = ["EXPLORE_SUBAGENT"]
