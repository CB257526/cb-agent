"""File edit tool.

Search/replace editing for existing text files, modelled after Claude Code's
FileEditTool but fitted to cb-agent's simpler Tool API. It reuses the same
read-before-write staleness guard as FileWriteTool.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.tool import Tool, ToolParameter
from tools.tools.file_state import get_read_state_registry
from tools.tools.file_write_tool import (
    MAX_WRITE_BYTES,
    _generate_unified_diff,
    _line_delta,
    FileWriteTool,
)


MAX_EDIT_FILE_SIZE = 10 * 1024 * 1024  # 10MB text file cap for in-memory edits

LEFT_SINGLE_CURLY_QUOTE = "\u2018"
RIGHT_SINGLE_CURLY_QUOTE = "\u2019"
LEFT_DOUBLE_CURLY_QUOTE = "\u201c"
RIGHT_DOUBLE_CURLY_QUOTE = "\u201d"


class FileEditTool(Tool):
    def __init__(self) -> None:
        super().__init__(
            name="file_edit",
            description=(
                "Edit a text file by replacing old_string with new_string. "
                "For existing files, first read the file with file_read so the "
                "staleness guard can prevent overwriting external changes. "
                "By default old_string must match exactly once; set replace_all=true "
                "to replace every occurrence. Use old_string='' only to create a new file."
            ),
        )

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="path",
                type="string",
                description="File path, absolute or relative to current BashSession.cwd.",
                required=True,
            ),
            ToolParameter(
                name="old_string",
                type="string",
                description=(
                    "Exact text to replace. Include enough surrounding context to make "
                    "the match unique. Use an empty string only when creating a new file."
                ),
                required=True,
            ),
            ToolParameter(
                name="new_string",
                type="string",
                description="Replacement text. Must differ from old_string.",
                required=True,
            ),
            ToolParameter(
                name="replace_all",
                type="boolean",
                description="Replace all occurrences of old_string. Default false.",
                required=False,
                default=False,
            ),
        ]

    def validate_parameters(self, parameters: Dict[str, Any]) -> bool:
        if not isinstance(parameters, dict):
            return False
        path = parameters.get("path")
        old_string = parameters.get("old_string")
        new_string = parameters.get("new_string")
        if not path or not isinstance(path, str):
            return False
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return False
        replace_all = parameters.get("replace_all")
        if replace_all is not None and not isinstance(replace_all, bool):
            return False
        return True

    def run(self, parameters: Dict[str, Any]) -> str:
        if not self.validate_parameters(parameters):
            return json.dumps(
                {"error": "parameter validation failed: need path, old_string, new_string"},
                ensure_ascii=False,
            )

        raw_path = parameters["path"]
        old_string: str = parameters["old_string"]
        new_string: str = parameters["new_string"]
        replace_all = bool(parameters.get("replace_all", False))

        if old_string == new_string:
            return json.dumps(
                {"error": "No changes to make: old_string and new_string are identical."},
                ensure_ascii=False,
            )

        if raw_path.startswith("\\\\") or raw_path.startswith("//"):
            return json.dumps(
                {"error": "UNC paths are disabled for Windows security.", "path": raw_path},
                ensure_ascii=False,
            )

        path_or_error = _resolve_path(raw_path)
        if isinstance(path_or_error, str):
            return json.dumps({"error": path_or_error}, ensure_ascii=False)
        p = path_or_error

        if p.exists():
            if not p.is_file():
                return json.dumps(
                    {"error": f"path exists but is not a file: {p}"},
                    ensure_ascii=False,
                )
            if p.suffix.lower() == ".ipynb":
                return json.dumps(
                    {"error": "Jupyter notebooks are not supported by file_edit.", "path": str(p)},
                    ensure_ascii=False,
                )
            try:
                size = p.stat().st_size
            except OSError as e:
                return json.dumps({"error": f"failed to stat file: {e}"}, ensure_ascii=False)
            if size > MAX_EDIT_FILE_SIZE:
                return json.dumps(
                    {
                        "error": (
                            f"file is too large to edit ({size} bytes). "
                            f"Maximum editable size is {MAX_EDIT_FILE_SIZE} bytes."
                        ),
                        "path": str(p),
                    },
                    ensure_ascii=False,
                )

            stale_error = _check_staleness(p)
            if stale_error is not None:
                return json.dumps(stale_error, ensure_ascii=False)

            try:
                old_content = _read_text_preserve_newlines(p)
            except OSError as e:
                return json.dumps({"error": f"failed to read file: {e}"}, ensure_ascii=False)
            file_type = "update"
        else:
            if old_string != "":
                return json.dumps(
                    {
                        "error": f"file does not exist: {p}. Use old_string='' to create it.",
                        "path": str(p),
                    },
                    ensure_ascii=False,
                )
            old_content = None
            file_type = "create"

        if old_content is None:
            new_content = new_string
            replacement_count = 1
            actual_old_string = ""
            actual_new_string = new_string
        else:
            line_ending = _detect_line_ending(old_content)
            edit_result = _apply_edit(
                old_content=_normalize_line_endings(old_content),
                old_string=_normalize_line_endings(old_string),
                new_string=_normalize_line_endings(new_string),
                replace_all=replace_all,
            )
            if "error" in edit_result:
                edit_result["path"] = str(p)
                return json.dumps(edit_result, ensure_ascii=False)
            new_content = _restore_line_endings(edit_result["content"], line_ending)
            replacement_count = int(edit_result["replacement_count"])
            actual_old_string = str(edit_result["actual_old_string"])
            actual_new_string = str(edit_result["actual_new_string"])

        size = len(new_content.encode("utf-8"))
        if size > MAX_WRITE_BYTES:
            return json.dumps(
                {
                    "error": (
                        f"edited content exceeds {MAX_WRITE_BYTES} bytes "
                        f"(actual {size}). Use smaller targeted edits."
                    ),
                    "path": str(p),
                },
                ensure_ascii=False,
            )

        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return json.dumps({"error": f"failed to create parent directory: {e}"}, ensure_ascii=False)

        try:
            FileWriteTool._atomic_write(p, new_content)
        except OSError as e:
            return json.dumps({"error": f"write failed: {e}", "path": str(p)}, ensure_ascii=False)

        get_read_state_registry().mark_read(p)

        added, removed = _line_delta(old_content, new_content)
        diff_text, diff_truncated, diff_total, diff_shown = _generate_unified_diff(
            old_content, new_content, str(p)
        )

        result: Dict[str, Any] = {
            "ok": True,
            "type": file_type,
            "path": str(p),
            "bytes_written": size,
            "replacements": replacement_count,
            "replace_all": replace_all,
            "lines_added": added,
            "lines_removed": removed,
            "message": (
                f"created {p}"
                if file_type == "create"
                else f"edited {p} ({replacement_count} replacement{'s' if replacement_count != 1 else ''}, +{added}/-{removed} lines)"
            ),
        }
        if actual_old_string != old_string:
            result["matched_string"] = actual_old_string
        if actual_new_string != new_string:
            result["applied_new_string"] = actual_new_string
        if diff_text:
            result["diff"] = diff_text
            result["diff_truncated"] = diff_truncated
            result["diff_lines_total"] = diff_total
            result["diff_lines_shown"] = diff_shown
        return json.dumps(result, ensure_ascii=False)


def _resolve_path(raw_path: str) -> Path | str:
    p = Path(raw_path).expanduser()
    if not p.is_absolute():
        from tools.tools.bash_session import get_session

        p = Path(get_session().cwd) / p
    try:
        return p.resolve()
    except OSError as e:
        return f"path resolution failed: {e}"


def _read_text_preserve_newlines(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace", newline="") as file:
        return file.read()


def _check_staleness(path: Path) -> Optional[Dict[str, Any]]:
    registry = get_read_state_registry()
    recorded_mtime = registry.get_read_mtime(path)
    if recorded_mtime is None:
        return {
            "error": (
                f"file exists but has not been read with file_read in this session. "
                f"Read it before file_edit to avoid overwriting unknown content. path={path}"
            ),
            "needs_read_first": True,
            "path": str(path),
        }
    try:
        current_mtime = path.stat().st_mtime_ns
    except OSError as e:
        return {"error": f"failed to read file metadata: {e}", "path": str(path)}
    if current_mtime > recorded_mtime:
        return {
            "error": "file has changed since the last file_read. Read it again before editing.",
            "stale": True,
            "path": str(path),
        }
    return None


def _apply_edit(
    *,
    old_content: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> Dict[str, Any]:
    if old_string == "":
        if old_content.strip() != "":
            return {"error": "Cannot create new file: file already exists and is not empty."}
        return {
            "content": new_string,
            "replacement_count": 1,
            "actual_old_string": "",
            "actual_new_string": new_string,
        }

    actual_old = _find_actual_string(old_content, old_string)
    if actual_old is None:
        return {
            "error": "String to replace not found in file.",
            "old_string": old_string,
        }

    matches = old_content.count(actual_old)
    if matches > 1 and not replace_all:
        return {
            "error": (
                f"Found {matches} matches of old_string, but replace_all is false. "
                "Provide more surrounding context or set replace_all=true."
            ),
            "matches": matches,
            "actual_old_string": actual_old,
        }

    actual_new = _preserve_quote_style(old_string, actual_old, new_string)
    if replace_all:
        new_content = old_content.replace(actual_old, actual_new)
        replacement_count = matches
    else:
        new_content = old_content.replace(actual_old, actual_new, 1)
        replacement_count = 1

    if new_content == old_content:
        return {"error": "Edit produced no file changes."}
    return {
        "content": new_content,
        "replacement_count": replacement_count,
        "actual_old_string": actual_old,
        "actual_new_string": actual_new,
    }


def _find_actual_string(file_content: str, search_string: str) -> Optional[str]:
    if search_string in file_content:
        return search_string

    normalized_file = _normalize_quotes(file_content)
    normalized_search = _normalize_quotes(search_string)
    index = normalized_file.find(normalized_search)
    if index != -1:
        return file_content[index: index + len(search_string)]

    ws_file = _normalize_whitespace(file_content)
    ws_search = _normalize_whitespace(search_string)
    index = ws_file.find(ws_search)
    if index != -1:
        return _map_normalized_match_back(file_content, index, len(ws_search))

    combined_file = _normalize_whitespace(normalized_file)
    combined_search = _normalize_whitespace(normalized_search)
    index = combined_file.find(combined_search)
    if index != -1:
        return _map_normalized_match_back(file_content, index, len(combined_search))
    return None


def _normalize_quotes(text: str) -> str:
    return (
        text.replace(LEFT_SINGLE_CURLY_QUOTE, "'")
        .replace(RIGHT_SINGLE_CURLY_QUOTE, "'")
        .replace(LEFT_DOUBLE_CURLY_QUOTE, '"')
        .replace(RIGHT_DOUBLE_CURLY_QUOTE, '"')
    )


def _normalize_line_endings(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_line_ending(text: str) -> str:
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def _restore_line_endings(text: str, line_ending: str) -> str:
    if line_ending == "\n":
        return text
    return text.replace("\n", line_ending)


def _normalize_whitespace(text: str) -> str:
    return text.replace("\t", "    ")


def _map_normalized_match_back(file_content: str, normalized_start: int, normalized_length: int) -> str:
    norm_pos = 0
    orig_pos = 0
    orig_start = -1
    orig_end = -1

    while orig_pos < len(file_content) and norm_pos <= normalized_start + normalized_length:
        if norm_pos == normalized_start:
            orig_start = orig_pos
        if norm_pos == normalized_start + normalized_length:
            orig_end = orig_pos
            break

        char = file_content[orig_pos]
        if char == "\t":
            next_norm_pos = norm_pos + 4
            if norm_pos < normalized_start < next_norm_pos and orig_start == -1:
                orig_start = orig_pos
            if norm_pos < normalized_start + normalized_length < next_norm_pos and orig_end == -1:
                orig_end = orig_pos + 1
            norm_pos = next_norm_pos
            orig_pos += 1
        else:
            norm_pos += 1
            orig_pos += 1

    if orig_start == -1:
        orig_start = 0
    if orig_end == -1:
        orig_end = min(len(file_content), orig_start + normalized_length)
    return file_content[orig_start:orig_end]


def _preserve_quote_style(old_string: str, actual_old_string: str, new_string: str) -> str:
    if old_string == actual_old_string:
        return new_string
    result = new_string
    if LEFT_DOUBLE_CURLY_QUOTE in actual_old_string or RIGHT_DOUBLE_CURLY_QUOTE in actual_old_string:
        result = _apply_curly_double_quotes(result)
    if LEFT_SINGLE_CURLY_QUOTE in actual_old_string or RIGHT_SINGLE_CURLY_QUOTE in actual_old_string:
        result = _apply_curly_single_quotes(result)
    return result


def _is_opening_context(chars: List[str], index: int) -> bool:
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in {" ", "\t", "\n", "\r", "(", "[", "{", "\u2014", "\u2013"}


def _apply_curly_double_quotes(text: str) -> str:
    chars = list(text)
    out: List[str] = []
    for i, char in enumerate(chars):
        if char == '"':
            out.append(LEFT_DOUBLE_CURLY_QUOTE if _is_opening_context(chars, i) else RIGHT_DOUBLE_CURLY_QUOTE)
        else:
            out.append(char)
    return "".join(out)


def _apply_curly_single_quotes(text: str) -> str:
    chars = list(text)
    out: List[str] = []
    for i, char in enumerate(chars):
        if char != "'":
            out.append(char)
            continue
        prev = chars[i - 1] if i > 0 else ""
        next_char = chars[i + 1] if i < len(chars) - 1 else ""
        if prev.isalpha() and next_char.isalpha():
            out.append(RIGHT_SINGLE_CURLY_QUOTE)
        else:
            out.append(LEFT_SINGLE_CURLY_QUOTE if _is_opening_context(chars, i) else RIGHT_SINGLE_CURLY_QUOTE)
    return "".join(out)
