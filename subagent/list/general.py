"""通用调查型子代理。"""

from subagent.models import SubagentDefinition, SubagentPermissionPolicy


GENERAL_SUBAGENT = SubagentDefinition(
    name="general",
    description="通用多步骤调查代理，适合资料整理、代码定位和综合分析。",
    system_prompt=(
        "你是 cb-agent 的通用调查子代理。只处理父 Agent 委派的任务，主动阅读和搜索必要资料，"
        "给出有证据的结论。你没有写入权限，不得修改文件或调用其他子代理。"
        "不要直接向用户提问；信息不足时说明采用的假设。最终报告应包含发现、证据位置和剩余风险。"
    ),
    tools=(
        "bash",
        "file_read",
        "glob",
        "grep",
        "knowledge_search",
        "list_tools",
        "load_image",
        "ls",
        "my_advanced_search",
    ),
    max_turns=30,
    permissions=SubagentPermissionPolicy(bash_mode="read_only"),
    source_path=__file__,
    builtin=True,
)


__all__ = ["GENERAL_SUBAGENT"]
