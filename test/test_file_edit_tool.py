from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.tools.bash_session import reset_session
from tools.tools.file_edit_tool import FileEditTool
from tools.tools.file_read_tool import FileReadTool
from tools.tools.file_state import get_read_state_registry


class TestFileEditTool(unittest.TestCase):
    def setUp(self) -> None:
        get_read_state_registry().clear()
        reset_session()
        self.tmp = Path(tempfile.mkdtemp())
        self.reader = FileReadTool()
        self.editor = FileEditTool()

    def _read(self, path: Path) -> None:
        result = json.loads(self.reader.run({"path": str(path), "head": 200}))
        self.assertNotIn("error", result)

    def test_create_new_file_with_empty_old_string(self) -> None:
        target = self.tmp / "sub" / "new.txt"

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "",
            "new_string": "hello\nworld\n",
        }))

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "create")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello\nworld\n")
        self.assertIn("diff", result)

    def test_existing_file_requires_prior_read(self) -> None:
        target = self.tmp / "exists.txt"
        target.write_text("alpha beta", encoding="utf-8")

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "beta",
            "new_string": "gamma",
        }))

        self.assertIn("error", result)
        self.assertTrue(result.get("needs_read_first"))
        self.assertEqual(target.read_text(encoding="utf-8"), "alpha beta")

    def test_unique_replacement_after_read(self) -> None:
        target = self.tmp / "code.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        self._read(target)

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "def f():\n    return 1",
            "new_string": "def f():\n    return 2",
        }))

        self.assertTrue(result["ok"])
        self.assertEqual(result["type"], "update")
        self.assertEqual(result["replacements"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "def f():\n    return 2\n")
        self.assertIn("-    return 1", result["diff"])
        self.assertIn("+    return 2", result["diff"])

    def test_multiple_matches_require_replace_all(self) -> None:
        target = self.tmp / "dupes.txt"
        target.write_text("x = 1\nx = 1\n", encoding="utf-8")
        self._read(target)

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "x = 1",
            "new_string": "x = 2",
        }))

        self.assertIn("error", result)
        self.assertEqual(result.get("matches"), 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 1\nx = 1\n")

    def test_replace_all(self) -> None:
        target = self.tmp / "dupes.txt"
        target.write_text("x = 1\nx = 1\n", encoding="utf-8")
        self._read(target)

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "x = 1",
            "new_string": "x = 2",
            "replace_all": True,
        }))

        self.assertTrue(result["ok"])
        self.assertEqual(result["replacements"], 2)
        self.assertEqual(target.read_text(encoding="utf-8"), "x = 2\nx = 2\n")

    def test_staleness_detected(self) -> None:
        target = self.tmp / "stale.txt"
        target.write_text("v1", encoding="utf-8")
        self._read(target)
        time.sleep(0.05)
        target.write_text("v2-external", encoding="utf-8")

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "v1",
            "new_string": "v3",
        }))

        self.assertIn("error", result)
        self.assertTrue(result.get("stale"))
        self.assertEqual(target.read_text(encoding="utf-8"), "v2-external")

    def test_unc_rejected(self) -> None:
        for unc in [r"\\server\share\x.txt", "//server/share/x.txt"]:
            result = json.loads(self.editor.run({
                "path": unc,
                "old_string": "",
                "new_string": "x",
            }))
            self.assertIn("error", result)

    def test_tab_indentation_match(self) -> None:
        target = self.tmp / "tabs.py"
        target.write_text("if ok:\n\treturn 1\n", encoding="utf-8")
        self._read(target)

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "if ok:\n    return 1",
            "new_string": "if ok:\n    return 2",
        }))

        self.assertTrue(result["ok"])
        self.assertEqual(target.read_text(encoding="utf-8"), "if ok:\n    return 2\n")

    def test_matches_lf_input_but_preserves_crlf_file(self) -> None:
        target = self.tmp / "crlf.txt"
        target.write_bytes(b"alpha\r\nbeta\r\n")
        self._read(target)

        result = json.loads(self.editor.run({
            "path": str(target),
            "old_string": "alpha\nbeta",
            "new_string": "alpha\ngamma",
        }))

        self.assertTrue(result["ok"])
        self.assertEqual(target.read_bytes(), b"alpha\r\ngamma\r\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
