"""compact 响应安装前的格式校验回归测试。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.compaction import CompactionError, run_local_compaction
from core.message import Message


class _Completions:
    """返回预设非流式响应的最小客户端。"""

    def __init__(self, message) -> None:
        self.message = message

    def create(self, **_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)])


def _run_with_message(message):
    """使用最小合法历史执行一次 compact。"""

    llm = SimpleNamespace(
        model="fake",
        client=SimpleNamespace(chat=SimpleNamespace(completions=_Completions(message))),
        output_token_param="none",
    )
    return run_local_compaction(
        llm=llm,
        system_message=None,
        history=[Message.create_user_message("继续当前任务")],
        hard_limit_tokens=100_000,
        estimate_request_tokens=lambda _messages: 100,
    )


def test_rejects_textual_dsml_tool_call_from_incident():
    """文本化 DSML 工具调用不能冒充摘要并替换完整 history。"""

    content = (
        "Another language model started to solve this problem.\n"
        "<｜｜DSML｜｜tool_calls><｜｜DSML｜｜invoke name=\"bash\">git status"
    )
    with pytest.raises(CompactionError, match="文本化工具调用"):
        _run_with_message(SimpleNamespace(content=content, tool_calls=None))


def test_rejects_structured_tool_call_response():
    """即使 provider 返回结构化 tool_calls，也必须保持旧历史不变。"""

    tool_call = SimpleNamespace(function=SimpleNamespace(name="bash", arguments="{}"))
    with pytest.raises(CompactionError, match="工具调用"):
        _run_with_message(SimpleNamespace(content=None, tool_calls=[tool_call]))


def test_accepts_plain_markdown_summary():
    """正常 Markdown 交接摘要仍按原路径安装。"""

    result = _run_with_message(
        SimpleNamespace(content="## 当前进度\n\n已完成排查，下一步运行测试。", tool_calls=None)
    )
    assert "已完成排查" in result.summary
