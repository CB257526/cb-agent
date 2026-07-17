"""agent/result_cap.py 单元测试。

覆盖场景：
1. 单条结果 < 上限 → 不截断
2. 单条结果超过 token 或字节上限 → 持久化 + preview 替换
3. 工具已自行持久化（output_file）→ 不重复持久化，复用路径
4. file_read 超限 → 保持结构化分页信息，只截断 content
5. 批量总量 > 上限 → 从最长开始逐条持久化
6. 持久化文件正确写入且内容可读
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from agent.result_cap import (
    MAX_BATCH_RESULT_BYTES,
    MAX_BATCH_RESULT_TOKENS,
    MAX_SINGLE_RESULT_BYTES,
    MAX_SINGLE_RESULT_TOKENS,
    PREVIEW_HEAD_CHARS,
    PREVIEW_TAIL_CHARS,
    cap_batch_results,
    cap_single_result,
)
from context.budget.tokens import count_tokens


@dataclass
class FakeToolCallResult:
    """模拟 ToolCallResult 的最小协议。"""
    call_id: str
    name: str
    result: str


class TestCapSingleResult(unittest.TestCase):
    """测试 cap_single_result 函数。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.persist_dir = Path(self.tmp_dir) / "tool_results"

    def test_under_limit_no_truncation(self):
        """未超限的结果原样返回。"""
        result = "x" * 1000
        capped, persisted = cap_single_result(
            result, "call_001", "bash", self.persist_dir,
        )
        self.assertEqual(capped, result)
        self.assertFalse(persisted)

    def test_at_limit_no_truncation(self):
        """刚好等于 token 上限且未超过字节兜底时不截断。"""
        result = "a" * MAX_SINGLE_RESULT_TOKENS
        with patch("agent.result_cap.count_tokens", side_effect=lambda text: len(text)):
            capped, persisted = cap_single_result(
                result, "call_002", "grep", self.persist_dir,
            )
        self.assertEqual(capped, result)
        self.assertFalse(persisted)

    def test_over_limit_persists_and_replaces(self):
        """超过字节兜底上限时持久化到磁盘并替换为 preview payload。"""
        result = "b" * (MAX_SINGLE_RESULT_BYTES + 5000)
        capped, persisted = cap_single_result(
            result, "call_003", "bash", self.persist_dir,
        )
        self.assertTrue(persisted)

        # 返回值是 JSON
        payload = json.loads(capped)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["tool_name"], "bash")
        self.assertEqual(payload["total_chars"], len(result))
        self.assertIn("total_tokens", payload)
        self.assertEqual(payload["total_bytes"], len(result.encode("utf-8")))
        self.assertIn("preview_head", payload)
        self.assertIn("preview_tail", payload)
        self.assertIn("persisted_path", payload)
        self.assertIn("hint", payload)
        self.assertIn("file_read", payload["hint"])

        # 持久化文件存在且内容完整
        persisted_file = Path(payload["persisted_path"])
        self.assertTrue(persisted_file.exists())
        content = persisted_file.read_text(encoding="utf-8")
        self.assertEqual(content, result)

    def test_existing_output_file_reused(self):
        """工具已自行持久化（output_file 字段）时不重复存盘。"""
        existing_path = "/tmp/fake_output.log"
        tool_result = json.dumps({
            "exit_code": 0,
            "stdout": "x" * (MAX_SINGLE_RESULT_BYTES + 1000),
            "output_file": existing_path,
            "output_truncated": True,
        }, ensure_ascii=False)

        capped, persisted = cap_single_result(
            tool_result, "call_004", "bash", self.persist_dir,
        )
        # 不应该触发 executor 层持久化
        self.assertFalse(persisted)

        # 返回的 payload 应复用已有路径
        payload = json.loads(capped)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["persisted_path"], existing_path)

    def test_file_read_persisted_result_no_double_persist(self):
        """file_read 超限时保持 JSON 与分页元数据，不复制原始文件。"""
        # 模拟 file_read 返回 JSON，path 指向 tool_results/
        fake_content = "测" * (MAX_SINGLE_RESULT_TOKENS + 2000)
        persisted_path = "/project/.cbagent/tool_results/call_prev.txt"
        tool_result = json.dumps({
            "path": persisted_path,
            "mode": "range-1-999",
            "content": fake_content,
            "total_lines": 999,
            "truncated": False,
        }, ensure_ascii=False)

        capped, persisted = cap_single_result(
            tool_result, "call_005", "file_read", self.persist_dir,
        )
        self.assertFalse(persisted)
        payload = json.loads(capped)
        self.assertEqual(payload["path"], persisted_path)
        self.assertTrue(payload["truncated"])
        self.assertTrue(payload["result_cap_truncated"])
        self.assertIn("start_line/end_line", payload["content"])
        self.assertLessEqual(len(capped.encode("utf-8")), MAX_SINGLE_RESULT_BYTES)

    def test_token_limit_triggers_before_byte_fallback(self):
        """token 密集文本即使未到 40K bytes，也必须按 10K token 上限处理。"""
        result = "x" * (MAX_SINGLE_RESULT_TOKENS + 1)
        with patch("agent.result_cap.count_tokens", side_effect=lambda text: len(text)):
            capped, persisted = cap_single_result(
                result, "call_token_dense", "search", self.persist_dir,
            )
        self.assertTrue(persisted)
        self.assertTrue(json.loads(capped)["truncated"])

    def test_byte_fallback_triggers_when_token_estimate_is_small(self):
        """token 估算偏小时，40K bytes 硬兜底仍能阻止超长结果进入上下文。"""
        result = " " * (MAX_SINGLE_RESULT_BYTES + 1)
        with patch("agent.result_cap.count_tokens", return_value=1):
            capped, persisted = cap_single_result(
                result, "call_byte_fallback", "search", self.persist_dir,
            )
        self.assertTrue(persisted)
        self.assertTrue(json.loads(capped)["truncated"])

    def test_preview_head_tail_content(self):
        """验证 preview 头尾内容正确。"""
        # 构造有辨识度的内容
        head_marker = "HEAD_START_" + "h" * PREVIEW_HEAD_CHARS
        tail_marker = "t" * PREVIEW_TAIL_CHARS + "_TAIL_END"
        middle = "m" * (MAX_SINGLE_RESULT_BYTES + 10000)
        result = head_marker + middle + tail_marker

        capped, persisted = cap_single_result(
            result, "call_006", "search", self.persist_dir,
        )
        self.assertTrue(persisted)

        payload = json.loads(capped)
        # 头部应以 HEAD_START_ 开头
        self.assertTrue(payload["preview_head"].startswith("HEAD_START_"))
        # 尾部应以 _TAIL_END 结尾
        self.assertTrue(payload["preview_tail"].endswith("_TAIL_END"))


class TestCapBatchResults(unittest.TestCase):
    """测试 cap_batch_results 函数。"""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.persist_dir = Path(self.tmp_dir) / "tool_results"

    def test_under_batch_limit_no_change(self):
        """批量总量未超限时不做任何修改。"""
        results = [
            FakeToolCallResult(call_id=f"c{i}", name="grep", result="x" * 1000)
            for i in range(5)
        ]
        original_results = [r.result for r in results]
        cap_batch_results(results, self.persist_dir)
        for i, r in enumerate(results):
            self.assertEqual(r.result, original_results[i])

    def test_over_batch_limit_truncates_longest_first(self):
        """批量总量超限时从最长的开始截断。"""
        # 制造总量超过 160K bytes 批量兜底的场景
        # 4 个结果，其中一个特别长
        # 空格序列占用 30K bytes 但 token 很少，便于单独验证字节批量兜底的处理顺序。
        short_result = " " * 30_000  # 30K bytes
        long_result = "L" * 120_000  # 120k
        results = [
            FakeToolCallResult(call_id="c1", name="bash", result=long_result),
            FakeToolCallResult(call_id="c2", name="grep", result=short_result),
            FakeToolCallResult(call_id="c3", name="grep", result=short_result),
            FakeToolCallResult(call_id="c4", name="grep", result=short_result),
        ]
        # 总量 = 120k + 30k*3 = 210k > 160k

        cap_batch_results(results, self.persist_dir)

        # 最长的那个应该被截断替换
        payload = json.loads(results[0].result)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total_chars"], 120_000)

        # 短的应该没变
        self.assertEqual(results[1].result, short_result)
        self.assertEqual(results[2].result, short_result)
        self.assertEqual(results[3].result, short_result)
        remaining_tokens = sum(count_tokens(r.result) for r in results)
        remaining_bytes = sum(len(r.result.encode("utf-8")) for r in results)
        self.assertLessEqual(remaining_tokens, MAX_BATCH_RESULT_TOKENS)
        self.assertLessEqual(remaining_bytes, MAX_BATCH_RESULT_BYTES)

    def test_already_truncated_skipped(self):
        """已经被 cap_single_result 处理过的结果不会被二次截断。"""
        already_capped = json.dumps({
            "truncated": True,
            "tool_name": "bash",
            "total_chars": 80000,
            "persisted_path": "/tmp/test.txt",
        }, ensure_ascii=False)

        results = [
            FakeToolCallResult(call_id="c1", name="bash", result=already_capped),
            FakeToolCallResult(call_id="c2", name="grep", result="x" * 180_000),
            FakeToolCallResult(call_id="c3", name="search", result="y" * 50_000),
        ]

        cap_batch_results(results, self.persist_dir)

        # 已截断的不应被再次处理
        self.assertEqual(results[0].result, already_capped)
        # 最长的 grep 结果应该被截断
        payload = json.loads(results[1].result)
        self.assertTrue(payload["truncated"])

    def test_persisted_file_readable(self):
        """批量截断时持久化的文件可以正确读取。"""
        big_result = "Z" * 150_000
        results = [
            FakeToolCallResult(call_id="c1", name="bash", result=big_result),
            FakeToolCallResult(call_id="c2", name="bash", result=big_result),
        ]
        # 总量 300k > 批量预算

        cap_batch_results(results, self.persist_dir)

        # 至少有一个被持久化
        truncated_count = 0
        for r in results:
            try:
                payload = json.loads(r.result)
                if payload.get("truncated"):
                    truncated_count += 1
                    persisted_file = Path(payload["persisted_path"])
                    self.assertTrue(persisted_file.exists())
                    content = persisted_file.read_text(encoding="utf-8")
                    self.assertEqual(content, big_result)
            except (json.JSONDecodeError, KeyError):
                pass
        self.assertGreater(truncated_count, 0)


if __name__ == "__main__":
    unittest.main()
