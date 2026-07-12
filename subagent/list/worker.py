"""受限实现型子代理。"""

from subagent.models import SubagentDefinition, SubagentPermissionPolicy


WORKER_SUBAGENT = SubagentDefinition(
    name="worker",
    description="受限代码实现代理，可在当前工作区内修改文件并执行已授权命令。",
    system_prompt=(
        "你是 cb-agent 的受限实现子代理。只修改委派任务明确涉及的文件，遵循仓库现有风格和测试惯例。"
        "所有写入必须局限于当前工作区；Bash 命令仍受父会话权限规则约束，无法交互审批时应停止该命令"
        "并报告，而不是绕过权限。不得调用其他子代理或通讯类工具。完成后报告修改、验证结果和剩余风险。"
    ),
    tools=(
        "bash",
        "file_edit",
        "file_read",
        "file_write",
        "glob",
        "grep",
        "list_tools",
        "load_image",
        "ls",
        "my_advanced_search",
        "todo",
    ),
    max_turns=40,
    permissions=SubagentPermissionPolicy(
        bash_mode="inherit",
        workspace_write=True,
    ),
    source_path=__file__,
    builtin=True,
)


__all__ = ["WORKER_SUBAGENT"]
