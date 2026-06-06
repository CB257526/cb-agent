from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.tools.bash_session import reset_session
from tools.tools.local_search import GlobTool, GrepTool, LsTool


class LocalSearchTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        reset_session(str(self.tmp))
        self.glob_tool = GlobTool()
        self.grep_tool = GrepTool()
        self.ls_tool = LsTool()

    def write(self, relative: str, content: str = "") -> Path:
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path


class TestGlobTool(LocalSearchTestCase):
    def test_glob_finds_files_sorted_by_recent_mtime(self) -> None:
        old_file = self.write("src/old.py", "print('old')\n")
        time.sleep(0.02)
        new_file = self.write("src/new.py", "print('new')\n")

        # 显式调整 mtime，避免不同文件系统时间精度导致排序测试偶发失败。
        now = time.time()
        os.utime(old_file, (now - 10, now - 10))
        os.utime(new_file, (now, now))

        result = json.loads(self.glob_tool.run({"pattern": "**/*.py", "path": "src"}))

        self.assertNotIn("error", result)
        self.assertEqual(result["files"][:2], ["src/new.py", "src/old.py"])
        self.assertEqual(result["num_files"], 2)
        self.assertFalse(result["truncated"])

    def test_glob_python_fallback_still_finds_files(self) -> None:
        self.write("pkg/a.py", "a = 1\n")
        self.write("pkg/b.txt", "b\n")

        with patch("tools.tools.local_search.shutil.which", return_value=None):
            result = json.loads(self.glob_tool.run({"pattern": "**/*.py"}))

        self.assertEqual(result["backend"], "python")
        self.assertEqual(result["files"], ["pkg/a.py"])


class TestGrepTool(LocalSearchTestCase):
    def test_grep_files_with_matches_filters_by_glob_and_case(self) -> None:
        self.write("src/app.py", "Error: broken\n")
        self.write("src/app.txt", "Error: ignored by glob\n")
        self.write("src/quiet.py", "nothing here\n")

        result = json.loads(self.grep_tool.run({
            "pattern": "error",
            "path": "src",
            "glob": "*.py",
            "case_insensitive": True,
        }))

        self.assertNotIn("error", result)
        self.assertEqual(result["mode"], "files_with_matches")
        self.assertEqual(result["files"], ["src/app.py"])
        self.assertEqual(result["num_files"], 1)

    def test_grep_content_mode_supports_pagination_and_context(self) -> None:
        self.write(
            "src/app.py",
            "line 1\nneedle one\nline 3\nneedle two\nline 5\n",
        )

        result = json.loads(self.grep_tool.run({
            "pattern": "needle",
            "path": "src",
            "output_mode": "content",
            "context": 0,
            "head_limit": 1,
            "offset": 1,
        }))

        self.assertEqual(result["mode"], "content")
        self.assertEqual(result["applied_limit"], 1)
        self.assertEqual(result["applied_offset"], 1)
        self.assertIn("src/app.py:4:needle two", result["content"])
        self.assertNotIn("needle one", result["content"])

    def test_grep_count_mode_reports_match_counts(self) -> None:
        self.write("src/app.py", "needle needle\nnone\nneedle\n")
        self.write("src/other.py", "needle\n")

        result = json.loads(self.grep_tool.run({
            "pattern": "needle",
            "path": "src",
            "output_mode": "count",
            "glob": "*.py",
        }))

        self.assertEqual(result["mode"], "count")
        self.assertEqual(result["num_files"], 2)
        self.assertEqual(result["num_matches"], 4)
        self.assertIn("src/app.py:3", result["content"])
        self.assertIn("src/other.py:1", result["content"])

    def test_grep_pattern_starting_with_dash_is_not_treated_as_option(self) -> None:
        self.write("src/flags.txt", "--force should be searched literally\n")

        result = json.loads(self.grep_tool.run({
            "pattern": "--force",
            "path": "src",
            "output_mode": "content",
        }))

        self.assertNotIn("error", result)
        self.assertIn("src/flags.txt:1:--force", result["content"])

    def test_grep_python_fallback_matches_content_shape(self) -> None:
        self.write("src/app.py", "Alpha\nbeta\n")

        with patch("tools.tools.local_search.shutil.which", return_value=None):
            result = json.loads(self.grep_tool.run({
                "pattern": "alpha",
                "path": "src",
                "output_mode": "content",
                "case_insensitive": True,
            }))

        self.assertEqual(result["backend"], "python")
        self.assertEqual(result["files"], ["src/app.py"])
        self.assertIn("src/app.py:1:Alpha", result["content"])


class TestLsTool(LocalSearchTestCase):
    def test_ls_respects_depth_limit_and_hidden_flag(self) -> None:
        self.write("src/app.py", "print('hi')\n")
        self.write("src/deep/nested.py", "print('deep')\n")
        self.write(".hidden/secret.txt", "secret\n")

        result = json.loads(self.ls_tool.run({"path": ".", "depth": 1}))

        self.assertNotIn("error", result)
        self.assertIn("src/", result["entries"])
        self.assertNotIn("src/app.py", result["entries"])
        self.assertNotIn(".hidden/", result["entries"])

        hidden_result = json.loads(self.ls_tool.run({
            "path": ".",
            "depth": 1,
            "include_hidden": True,
        }))
        self.assertIn(".hidden/", hidden_result["entries"])

    def test_ls_returns_truncated_when_limit_is_hit(self) -> None:
        self.write("a.txt", "a\n")
        self.write("b.txt", "b\n")
        self.write("c.txt", "c\n")

        result = json.loads(self.ls_tool.run({"path": ".", "depth": 1, "limit": 2}))

        self.assertEqual(len(result["entries"]), 2)
        self.assertGreater(result["total_entries"], len(result["entries"]))
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
