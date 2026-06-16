"""agent/result_cap.py 单元测试。

覆盖场景：
1. 单条结果 < 上限 → 不截断
2. 单条结果 > 上限 → 持久化 + preview 替换
3. 工具已自行持久化（output_file）→ 不重复持久化，复用路径
4. file_read 读取持久化文件 → 防循环，只 inline 截断
5. 批量总量 > 上限 → 从最长开始逐条持久化
6. 持久化文件正确写入且内容可读
"""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.result_cap import (
    MAX_BATCH_RESULT_CHARS,
    MAX_SINGLE_RESULT_CHARS,
    PERSIST_DIR_MARKER,
    PREVIEW_HEAD_CHARS,
    PREVIEW_TAIL_CHARS,
    cap_batch_results,
    cap_single_result,
)


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
        """刚好等于上限时不截断。"""
        result = "a" * MAX_SINGLE_RESULT_CHARS
        capped, persisted = cap_single_result(
            result, "call_002", "grep", self.persist_dir,
        )
        self.assertEqual(capped, result)
        self.assertFalse(persisted)

    def test_over_limit_persists_and_replaces(self):
        """超限时持久化到磁盘并替换为 preview payload。"""
        result = "b" * (MAX_SINGLE_RESULT_CHARS + 5000)
        capped, persisted = cap_single_result(
            result, "call_003", "bash", self.persist_dir,
        )
        self.assertTrue(persisted)

        # 返回值是 JSON
        payload = json.loads(capped)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["tool_name"], "bash")
        self.assertEqual(payload["total_chars"], len(result))
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
            "stdout": "x" * (MAX_SINGLE_RESULT_CHARS + 1000),
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
        """file_read 读取 tool_results/ 下的文件时，只 inline 截断不持久化。"""
        # 模拟 file_read 返回 JSON，path 指向 tool_results/
        fake_content = "c" * (MAX_SINGLE_RESULT_CHARS + 2000)
        tool_result = json.dumps({
            "path": f"/project/.cbagent/{PERSIST_DIR_MARKER}call_prev.txt",
            "content": fake_content,
            "total_lines": 999,
            "truncated": False,
        }, ensure_ascii=False)

        capped, persisted = cap_single_result(
            tool_result, "call_005", "file_read", self.persist_dir,
        )
        # 不应持久化
        self.assertFalse(persisted)
        # 应做 inline 截断
        self.assertIn("已截断", capped)
        self.assertIn("start_line/end_line", capped)
        # 长度应在上限附近
        self.assertLessEqual(
            len(capped),
            MAX_SINGLE_RESULT_CHARS + 100,  # 加上截断提示的长度
        )

    def test_preview_head_tail_content(self):
        """验证 preview 头尾内容正确。"""
        # 构造有辨识度的内容
        head_marker = "HEAD_START_" + "h" * PREVIEW_HEAD_CHARS
        tail_marker = "t" * PREVIEW_TAIL_CHARS + "_TAIL_END"
        middle = "m" * (MAX_SINGLE_RESULT_CHARS + 10000)
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
        # 制造总量刚好超 200k 的场景
        # 4 个结果，其中一个特别长
        short_result = "s" * 30_000  # 30k
        long_result = "L" * 120_000  # 120k
        results = [
            FakeToolCallResult(call_id="c1", name="bash", result=long_result),
            FakeToolCallResult(call_id="c2", name="grep", result=short_result),
            FakeToolCallResult(call_id="c3", name="grep", result=short_result),
            FakeToolCallResult(call_id="c4", name="grep", result=short_result),
        ]
        # 总量 = 120k + 30k*3 = 210k > 200k

        cap_batch_results(results, self.persist_dir)

        # 最长的那个应该被截断替换
        payload = json.loads(results[0].result)
        self.assertTrue(payload["truncated"])
        self.assertEqual(payload["total_chars"], 120_000)

        # 短的应该没变
        self.assertEqual(results[1].result, short_result)
        self.assertEqual(results[2].result, short_result)
        self.assertEqual(results[3].result, short_result)

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
        # 总量 300k > 200k

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
