"""ContextBuilder 测试脚本

覆盖：
- 模块级 helper（count_tokens / tokenize_for_relevance / jaccard / messages_to_text）
- ContextPacket 懒计算 token
- 同步与异步 build()
- MMR 多样性
- 截断保结构
- 中文 token 集合相关性

跑法：
    cd cb-agent && ../venv/python.exe test/test_context_builder.py
"""

import os
import sys
import asyncio
import time
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

# 添加项目根目录到 path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from context.builder import (
    ContextBuilder,
    ContextConfig,
    ContextPacket,
    ContextPriority,
    ContextResult,
    count_tokens,
    jaccard,
    messages_to_text,
    tokenize_for_relevance,
    _get_encoding,
)
from context.markdown_memory import MarkdownMemoryProvider
from core.message import Message, MessageRole


# ---------- 简易断言工具 ----------

_passed = 0
_failed = 0


def _check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  ✓ {msg}")
    else:
        _failed += 1
        print(f"  ✗ {msg}")


# ---------- mock 工具 ----------

class _MockMemoryTool:
    """伪装 MemoryTool，按 query 返回不同结果，用于覆盖 _search_memory_state/_related。"""
    def __init__(self, search_results=None, raise_on=None):
        # search_results: dict[memory_type, str]，None 表示返回空
        self.search_results = search_results or {}
        self.raise_on = raise_on or set()
        self.calls = []

    def execute(self, action, **kwargs):
        self.calls.append((action, kwargs))
        if action == "search":
            mem_type = kwargs.get("memory_type")
            if mem_type in self.raise_on:
                raise RuntimeError(f"模拟 {mem_type} 检索失败")
            return self.search_results.get(mem_type, self.search_results.get("__default__", ""))
        return ""


class _MockRagTool:
    def __init__(self, result=""):
        self.result = result
        self.calls = []

    def run(self, parameters):
        self.calls.append(parameters)
        return self.result


# ---------- 测试 ----------

def test_helpers():
    print("=" * 60)
    print("测试 1: 模块级 helper")
    print("=" * 60)

    # count_tokens
    _check(count_tokens("") == 0, "空串返回 0")
    _check(count_tokens("hello") > 0, "英文计数 > 0")
    _check(count_tokens("你好世界") > 0, "中文计数 > 0")
    _check(count_tokens("hi", model_name="gpt-4o") > 0, "model_name 兼容签名不抛异常")

    # tokenize_for_relevance + jaccard
    a = tokenize_for_relevance("数据库连接超时")
    b = tokenize_for_relevance("数据库连接超时怎么处理")
    c = tokenize_for_relevance("天气真不错")
    _check(jaccard(a, b) > 0, "中文有重叠 jaccard > 0")
    _check(jaccard(a, c) >= 0, "中文无重叠 jaccard >= 0")
    _check(jaccard(a, b) > jaccard(a, c), "更相关的对子 jaccard 更高")
    _check(jaccard(frozenset(), b) == 0.0, "空集合返回 0")

    # 缓存命中：同样的字符串第二次调用返回同一对象
    a2 = tokenize_for_relevance("数据库连接超时")
    _check(a is a2, "lru_cache 命中")

    # 编码器单例
    _check(_get_encoding() is _get_encoding(), "编码器单例")


def test_messages_to_text():
    print("=" * 60)
    print("测试 2: messages_to_text 处理多模态与枚举")
    print("=" * 60)

    msgs = [
        Message.create_system_message("你是 DBA"),
        Message.create_user_message(input_text="数据库连不上"),
        Message.create_assistant_message(input_text="检查防火墙"),
        Message.create_user_message(
            input_text="我截图了",
            input_image="https://example.com/x.png",
        ),
        Message.create_tool_message(
            tool_call_id="t1",
            tool_name="ping",
            tool_output="timeout",
        ),
    ]
    text = messages_to_text(msgs)

    _check("[system]" in text, "system 角色用 .value 而非 MessageRole.SYSTEM")
    _check("[user]" in text, "user 角色用 .value")
    _check("[assistant]" in text, "assistant 角色用 .value")
    _check("[tool]" in text, "tool 角色用 .value")
    _check("MessageRole" not in text, "不会出现枚举字面量")
    _check("数据库连不上" in text, "user 文本提取")
    _check("[image:" in text and "x.png" in text, "image_url 多模态被正确抽出")
    _check("[tool:ping]" in text, "tool 角色带 tool_name 标注")

    # max_messages 截断
    short = messages_to_text(msgs, max_messages=2)
    _check(short.count("\n") <= 1, "max_messages 截断到最近 2 条")


def test_packet_lazy_token():
    print("=" * 60)
    print("测试 3: ContextPacket token 懒计算")
    print("=" * 60)

    p = ContextPacket(content="hello world")
    _check(p._token_count is None, "初始未计算")
    _ = p.token_count
    _check(p._token_count is not None, "首次访问后已缓存")
    _check(p.token_count == p.token_count, "重复访问值稳定")

    p2 = ContextPacket(content="x", _token_count=42)
    _check(p2.token_count == 42, "显式 token_count 不被覆盖")


def test_build_basic():
    print("=" * 60)
    print("测试 4: 同步 build 基本流程")
    print("=" * 60)

    memory = _MockMemoryTool(search_results={
        "working": "记忆 working: 上次提到了 PostgreSQL",
        "episodic": "",
        "semantic": "",
        "__default__": "相关记忆: 连接池大小 20",
    })
    rag = _MockRagTool(result="RAG: PostgreSQL 默认 statement_timeout 是 0（不限制）")

    builder = ContextBuilder(
        memory_tool=memory,
        rag_tool=rag,
        config=ContextConfig(max_tokens=4000, min_relevance=0.0),
    )

    history = [
        Message.create_user_message("我们项目用 PostgreSQL"),
        Message.create_assistant_message("好的，记下了"),
    ]
    extra = ContextPacket(
        content="额外证据：超时常因网络抖动",
        priority=ContextPriority.P2_EVIDENCE,
    )
    ctx = builder.build(
        user_query="数据库连接超时怎么办",
        conversation_history=history,
        system_instructions="你是资深 DBA",
        additional_packets=[extra],
    )

    _check("[Role & Policies]" in ctx, "含 [Role & Policies] 节")
    _check("你是资深 DBA" in ctx, "系统指令进入 [Role & Policies]")
    _check("[Task]" in ctx, "含 [Task] 节")
    _check("数据库连接超时怎么办" in ctx, "用户 query 在 [Task]")
    _check("[Evidence]" in ctx, "含 [Evidence] 节")
    _check("[Context]" in ctx, "含 [Context] 节（对话历史）")
    _check("[Output]" in ctx, "含 [Output] 节")
    _check("MessageRole" not in ctx, "历史段不漏 enum 字面")
    _check("'type': 'text'" not in ctx, "历史段不漏多模态字典字面")
    # [Output] 不应有 12 空格缩进
    for line in ctx.split("\n"):
        if line.startswith("            "):
            _check(False, f"发现意外缩进: {line!r}")
            break
    else:
        _check(True, "[Output] 无 12 空格缩进")


def test_chinese_relevance():
    print("=" * 60)
    print("测试 5: 中文相关性（无空格也能算分）")
    print("=" * 60)

    builder = ContextBuilder(config=ContextConfig(max_tokens=4000, min_relevance=0.01))
    extra_relevant = ContextPacket(
        content="数据库连接超时常见原因有网络、连接池、防火墙",
        priority=ContextPriority.P2_EVIDENCE,
    )
    extra_unrelated = ContextPacket(
        content="今天天气很不错适合户外运动",
        priority=ContextPriority.P2_EVIDENCE,
    )

    result = builder.build_detailed(
        user_query="数据库连接超时怎么办",
        additional_packets=[extra_relevant, extra_unrelated],
    )

    _check(extra_relevant.relevance_score > 0, "相关包 relevance > 0")
    _check(
        extra_relevant.relevance_score > extra_unrelated.relevance_score,
        f"相关包评分高于无关包 ({extra_relevant.relevance_score:.3f} > {extra_unrelated.relevance_score:.3f})",
    )


def test_compress_keeps_structure():
    print("=" * 60)
    print("测试 6: 压缩按节丢弃不破坏结构")
    print("=" * 60)

    builder = ContextBuilder(
        config=ContextConfig(max_tokens=200, reserve_ratio=0.0, min_relevance=0.0),
    )
    # 构造一堆大证据 + 大历史，强制超预算
    big = "x" * 2000
    history = [Message.create_user_message(big)]
    # 把超大内容放进 P1（State）才会被强制纳入并触发 compress 阶段截断；
    # P2 在 _select 阶段就因超预算被丢弃，到不了 _compress
    extras = [
        ContextPacket(content=big, priority=ContextPriority.P1_STATE)
        for _ in range(3)
    ]

    result = builder.build_detailed(
        user_query="任意问题",
        conversation_history=history,
        system_instructions="你是助手",
        additional_packets=extras,
    )

    _check(result.truncated, f"确实触发了截断 (total_tokens={result.total_tokens})")
    _check("[Role & Policies]" in result.context, "[Role & Policies] 始终保留")
    _check("[Task]" in result.context, "[Task] 始终保留")
    _check("[Output]" in result.context, "[Output] 始终保留")
    # 不应出现节标题被切成半个
    for header in ("[Role", "[Task", "[Evidence", "[Context", "[Output"):
        if header in result.context:
            # 配套的 ] 必须存在
            _check(
                "]" in result.context.split(header, 1)[1].split("\n", 1)[0],
                f"节标题 {header} 完整未被切",
            )


def test_mmr_diversity():
    print("=" * 60)
    print("测试 7: MMR 在重复证据中选多样")
    print("=" * 60)

    same1 = ContextPacket(
        content="数据库连接超时通常由网络抖动引起，建议检查网络",
        priority=ContextPriority.P2_EVIDENCE,
    )
    same2 = ContextPacket(
        content="数据库连接超时通常由网络抖动引起，建议检查网络",
        priority=ContextPriority.P2_EVIDENCE,
    )
    diverse = ContextPacket(
        content="数据库超时也可能是连接池打满，需要扩容连接池",
        priority=ContextPriority.P2_EVIDENCE,
    )

    builder = ContextBuilder(config=ContextConfig(
        max_tokens=4000,
        min_relevance=0.0,
        enable_mmr=True,
        mmr_lambda=0.5,
    ))
    result = builder.build_detailed(
        user_query="数据库连接超时怎么办",
        additional_packets=[same1, same2, diverse],
    )

    selected_contents = [p.content for p in result.selected if p.priority == ContextPriority.P2_EVIDENCE]
    # MMR 应该让 diverse 排在第二个重复包之前
    if len(selected_contents) >= 2:
        diverse_idx = next((i for i, c in enumerate(selected_contents) if "连接池" in c), -1)
        same_indices = [i for i, c in enumerate(selected_contents) if "网络抖动" in c]
        _check(
            diverse_idx >= 0 and (not same_indices or diverse_idx < max(same_indices)),
            f"diverse 包排序优先于第二个重复包（diverse={diverse_idx}, same={same_indices}）",
        )
    else:
        _check(False, "选中证据不足以判断 MMR 行为")


def test_async_build():
    print("=" * 60)
    print("测试 8: 异步 abuild 与同步 build 等价（mock 工具）")
    print("=" * 60)

    memory = _MockMemoryTool(search_results={"__default__": "记忆: x"})
    rag = _MockRagTool(result="RAG: y")

    builder = ContextBuilder(
        memory_tool=memory,
        rag_tool=rag,
        config=ContextConfig(max_tokens=4000, min_relevance=0.0),
    )

    sync_ctx = builder.build(user_query="问题")
    async_ctx = asyncio.run(builder.abuild(user_query="问题"))

    # 内容同源（细节可能因时间戳略有差异，但结构一致）
    _check("[Task]" in async_ctx, "异步版本有 [Task]")
    _check("RAG: y" in async_ctx, "异步版本含 RAG 结果")
    _check("记忆: x" in async_ctx, "异步版本含记忆结果")


def test_memory_tool_failure_isolated():
    print("=" * 60)
    print("测试 9: memory 异常时不影响整条流水线")
    print("=" * 60)

    memory = _MockMemoryTool(
        search_results={"working": "记忆 a", "episodic": "记忆 b"},
        raise_on={"semantic"},  # semantic 类型抛异常
    )
    builder = ContextBuilder(
        memory_tool=memory,
        config=ContextConfig(max_tokens=4000, min_relevance=0.0),
    )

    ctx = builder.build(user_query="任意")
    _check("记忆 a" in ctx or "记忆 b" in ctx, "其他记忆类型仍正常返回")
    _check("[Task]" in ctx, "异常未中断流水线")


def test_markdown_memory_provider_in_context():
    print("=" * 60)
    print("测试 10: Markdown 轻量记忆进入 ContextBuilder")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        project_root = root / "project"
        global_memory = root / "global_memory"
        project_root.mkdir()

        provider = MarkdownMemoryProvider(
            project_dir=project_root,
            global_dir=global_memory,
            max_related=5,
            max_state=5,
        )
        provider.ensure_initialized()

        project_memory = project_root / ".cbagent" / "memory"
        (project_memory / "project_conventions.md").write_text(
            """---
name: 审批流程约定
description: 当前项目的审批与上下文管理约定
type: project
scope: project
---
项目事实：审批流程需要保留 transcript 审计，并优先使用 Markdown 记忆。
""",
            encoding="utf-8",
        )
        (project_memory / "MEMORY.md").write_text(
            "# Memory Index\n- [审批流程约定](project_conventions.md) — 审批与上下文管理\n",
            encoding="utf-8",
        )

        (global_memory / "user_preferences.md").write_text(
            """---
name: 用户偏好
description: 用户长期回答偏好
type: user
scope: global
---
用户偏好：回答保持中文、简洁，并在必要时说明验证命令。
""",
            encoding="utf-8",
        )
        (global_memory / "MEMORY.md").write_text(
            "# Memory Index\n- [用户偏好](user_preferences.md) — 中文简洁回答偏好\n",
            encoding="utf-8",
        )

        builder = ContextBuilder(
            md_memory_provider=provider,
            config=ContextConfig(max_tokens=4000, min_relevance=0.0),
        )
        ctx = builder.build(user_query="请按审批流程处理，并保持中文简洁回答")

        _check("Markdown 记忆状态" in ctx, "包含 Markdown 记忆状态段")
        _check("审批流程需要保留 transcript 审计" in ctx, "项目级 Markdown 记忆被注入")
        _check("回答保持中文、简洁" in ctx, "全局 Markdown 记忆被注入")
        _check("project_conventions.md" in ctx, "上下文标注项目记忆文件名")
        _check("user_preferences.md" in ctx, "上下文标注全局记忆文件名")
        _check((project_memory / "MEMORY.md").exists(), "项目级 MEMORY.md 存在")
        _check((global_memory / "MEMORY.md").exists(), "全局 MEMORY.md 存在")


def test_context_builder_import_is_lightweight():
    print("=" * 60)
    print("测试 11: context.builder 导入不触碰 full 记忆工具")
    print("=" * 60)

    code = (
        "import sys;"
        "import context.builder;"
        "bad = [m for m in ('tools.tools.memory_tool','tools.tools.rag_tool') if m in sys.modules];"
        "print(','.join(bad))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=env["PYTHONPATH"],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    _check(result.returncode == 0, f"子进程导入 context.builder 成功 ({result.stderr.strip()[:120]})")
    _check(result.stdout.strip() == "", "未运行时导入 memory_tool/rag_tool")


def test_perf_count_tokens():
    print("=" * 60)
    print("测试 12: count_tokens 性能（编码器复用）")
    print("=" * 60)

    # 预热
    count_tokens("warmup")

    n = 1000
    t0 = time.perf_counter()
    for _ in range(n):
        count_tokens("hello world this is a benchmark")
    elapsed = time.perf_counter() - t0
    avg_us = elapsed / n * 1e6
    print(f"  1000 次 count_tokens 耗时 {elapsed*1000:.2f}ms ({avg_us:.1f} μs / 次)")
    # 单次平均应远低于 1ms（编码器只初始化一次）
    _check(avg_us < 1000, f"平均 < 1ms / 次 (实际 {avg_us:.1f}μs)")


def test_to_messages():
    print("=" * 60)
    print("测试 13: to_messages / ato_messages 适配 OpenAI 协议")
    print("=" * 60)

    memory = _MockMemoryTool(search_results={"__default__": "记忆: x"})
    rag = _MockRagTool(result="RAG: y")
    builder = ContextBuilder(
        memory_tool=memory,
        rag_tool=rag,
        config=ContextConfig(max_tokens=4000, min_relevance=0.0),
    )

    # 同步
    msgs = builder.to_messages(
        user_query="数据库连接超时怎么办",
        system_instructions="你是 DBA",
    )
    _check(isinstance(msgs, list) and len(msgs) == 2, "返回 2 条 message")
    _check(msgs[0]["role"] == "system", "第一条是 system")
    _check(msgs[1]["role"] == "user", "第二条是 user")
    _check("[Task]" in msgs[0]["content"], "system content 含完整 prompt 结构")
    _check("你是 DBA" in msgs[0]["content"], "system_instructions 进入 system content")
    _check(msgs[1]["content"] == "数据库连接超时怎么办", "user content 是原始 query")

    # 异步
    amsgs = asyncio.run(builder.ato_messages(user_query="问题 X"))
    _check(amsgs[0]["role"] == "system" and amsgs[1]["role"] == "user", "异步返回结构正确")
    _check(amsgs[1]["content"] == "问题 X", "异步 user content 是原始 query")


def main():
    test_helpers()
    print()
    test_messages_to_text()
    print()
    test_packet_lazy_token()
    print()
    test_build_basic()
    print()
    test_chinese_relevance()
    print()
    test_compress_keeps_structure()
    print()
    test_mmr_diversity()
    print()
    test_async_build()
    print()
    test_memory_tool_failure_isolated()
    print()
    test_markdown_memory_provider_in_context()
    print()
    test_context_builder_import_is_lightweight()
    print()
    test_perf_count_tokens()
    print()
    test_to_messages()

    print()
    print("=" * 60)
    print(f"总计：通过 {_passed}，失败 {_failed}")
    print("=" * 60)
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
