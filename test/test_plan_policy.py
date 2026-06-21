"""Plan Mode 服务端工具执行策略单元测试。

覆盖：
- is_plan_readonly_bash(): 只读命令检查（允许/拒绝/边界情况）
- PlanExecutionPolicy.check(): 工具级别的允许/拒绝判断
- PlanExecutionPolicy.denied_result(): 拒绝响应的 JSON 结构
"""

import json

from agent.plan_policy import PlanExecutionPolicy, is_plan_readonly_bash


def assert_allowed(command: str):
    """断言 bash 命令在 Plan Mode 下被允许（只读、无副作用）。"""
    ok, reason = is_plan_readonly_bash(command)
    assert ok, reason


def assert_denied(command: str):
    """断言 bash 命令在 Plan Mode 下被拒绝，且 reason 非空。"""
    ok, reason = is_plan_readonly_bash(command)
    assert not ok
    assert reason


def test_plan_readonly_bash_allows_exploration_commands():
    """验证常见探索性命令被允许。

    包括：ls, rg, git diff, Get-Content (PowerShell) 等。
    同时验证 rg "A&B" 不会被误判为后台操作符（区分 && 与裸 &）。
    """
    assert_allowed("ls")
    assert_allowed("rg foo .")
    assert_allowed('rg "A&B" .')  # 引号内的 & 不应触发后台检测
    assert_allowed("git diff")
    assert_allowed("Get-Content file.txt")


def test_plan_readonly_bash_denies_mutating_commands():
    """验证写入/修改命令被拒绝。

    覆盖：输出重定向(>), rm, sed -i, git checkout, npm install,
    git diff --output, sort -o（写到文件的 flag）。
    """
    assert_denied("echo x > a.txt")
    assert_denied("rm file")
    assert_denied("sed -i s/a/b/ file.txt")
    assert_denied("git checkout main")
    assert_denied("npm install")
    assert_denied("git diff --output patch.txt")
    assert_denied("sort input.txt -o output.txt")


def test_plan_readonly_bash_denies_background_flag():
    """验证 run_in_background=True 时即使命令是只读的也会被拒绝。

    Plan Mode 不允许后台执行，因为后台任务无法受策略管控。
    """
    ok, reason = is_plan_readonly_bash("ls", run_in_background=True)
    assert not ok
    assert "background" in reason


def test_plan_readonly_bash_denies_shell_background_operators():
    """验证 shell 级别的后台操作符被拒绝。

    "ls &" 中的裸 &（非 &&）会被 _has_unquoted_single_ampersand 检测。
    Start-Job 被 RAW_DENY_PATTERNS 正则匹配。
    """
    assert_denied("ls &")
    assert_denied("tail -f app.log &")
    assert_denied("Start-Job { Get-ChildItem }")


def test_plan_execution_policy_allows_only_read_actions():
    """验证 PlanExecutionPolicy 的工具级别过滤。

    - file_read / rag search → 允许
    - rag add_document / file_write → 拒绝，reason 包含 "not allowed"
    """
    policy = PlanExecutionPolicy()

    assert policy.check("file_read", {"path": "a.py"}) == (True, None)
    assert policy.check("rag", {"action": "search", "query": "x"}) == (True, None)

    ok, reason = policy.check("rag", {"action": "add_document"})
    assert not ok
    assert "not allowed" in (reason or "")

    ok, reason = policy.check("file_write", {"path": "a.py", "content": "x"})
    assert not ok
    assert "not allowed" in (reason or "")


def test_plan_execution_policy_denied_result_is_structured_json():
    """验证 denied_result 返回结构化 JSON（非空字符串）。

    必需字段：plan_mode_denied=True, tool, reason, command（bash 时）。
    这个 JSON 会作为 tool_result 回灌给 LLM，让模型知道"被拒绝了"。
    """
    policy = PlanExecutionPolicy()
    payload = json.loads(policy.denied_result("bash", {"command": "rm file"}, "mutating"))

    assert payload["plan_mode_denied"] is True
    assert payload["tool"] == "bash"
    assert payload["reason"] == "mutating"
    assert payload["command"] == "rm file"
