"""Memory @include 解析与 MemoryLoader 路径优先级单元测试。

覆盖:
- @include 在 fenced/inline code 内不被命中
- HTML 注释剥离
- 路径展开(./, ~, 绝对路径)
- 循环检测(A->B->A 不死循环)
- 深度上限
- frontmatter 解析
- format_memory_files 输出包含 MEMORY_INSTRUCTION_PROMPT 和文件路径
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from context.memory.formatter import MEMORY_INSTRUCTION_PROMPT, format_memory_files
from context.memory.frontmatter import parse_frontmatter, strip_block_html_comments
from context.memory.include_resolver import extract_include_paths
from context.memory.loader import MemoryLoader, _process_memory_file
from context.memory.types import MemoryFileInfo


def test_include_simple_path(tmp_path: Path):
    base = tmp_path / "main.md"
    base.write_text("see @./other.md for details", encoding="utf-8")
    out = extract_include_paths(base.read_text(encoding="utf-8"), base)
    assert (tmp_path / "other.md").resolve() in out


def test_include_skipped_in_fenced_code(tmp_path: Path):
    base = tmp_path / "main.md"
    body = (
        "Real include: @./real.md\n\n"
        "```python\n"
        "x = '@./fake.md'\n"
        "```\n"
    )
    base.write_text(body, encoding="utf-8")
    out = extract_include_paths(body, base)
    paths = {str(p) for p in out}
    assert any("real.md" in p for p in paths)
    assert not any("fake.md" in p for p in paths)


def test_include_skipped_in_inline_code():
    base = Path("/tmp/main.md")
    body = "Use `@./code-mention.md` syntax. Real: @./real.md"
    out = extract_include_paths(body, base)
    paths = {str(p) for p in out}
    assert any("real.md" in p for p in paths)
    assert not any("code-mention.md" in p for p in paths)


def test_include_strips_trailing_punctuation():
    base = Path("/tmp/main.md")
    body = "see @./guide.md."  # 末尾句号
    out = extract_include_paths(body, base)
    paths = [str(p) for p in out]
    assert any(p.endswith("guide.md") for p in paths)
    assert not any(p.endswith("guide.md.") for p in paths)


def test_strip_block_html_comments_basic():
    src = "before <!-- hidden\nblock --> after"
    out = strip_block_html_comments(src)
    assert "hidden" not in out
    assert "before" in out
    assert "after" in out


def test_parse_frontmatter_basic():
    src = "---\nname: test\ntype: project\n---\nbody text"
    meta, body = parse_frontmatter(src)
    assert meta == {"name": "test", "type": "project"}
    assert body == "body text"


def test_parse_frontmatter_no_block():
    src = "no frontmatter here"
    meta, body = parse_frontmatter(src)
    assert meta == {}
    assert body == "no frontmatter here"


def test_parse_frontmatter_unclosed_block():
    src = "---\nname: x\nno closing fence"
    meta, body = parse_frontmatter(src)
    # 未闭合 -> 全文当 body
    assert meta == {}
    assert "no closing fence" in body


def test_process_memory_file_loops_dont_recurse(tmp_path: Path):
    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("@./b.md", encoding="utf-8")
    b.write_text("@./a.md", encoding="utf-8")

    out = asyncio.run(_process_memory_file(a, "Project", processed=set()))
    paths = [str(f.path) for f in out]
    # A B 各被处理一次
    assert sum(p.endswith("a.md") for p in paths) == 1
    assert sum(p.endswith("b.md") for p in paths) == 1


def test_process_memory_file_depth_limit(tmp_path: Path):
    chain = []
    for i in range(8):
        f = tmp_path / f"d{i}.md"
        chain.append(f)
    for i in range(7):
        chain[i].write_text(f"@./d{i+1}.md", encoding="utf-8")
    chain[7].write_text("leaf body", encoding="utf-8")

    out = asyncio.run(_process_memory_file(chain[0], "Project", processed=set()))
    # MAX_INCLUDE_DEPTH=5 -> 顶层 + 5 层 include = 6 文件,深 6/7 不会加载
    assert len(out) <= 6


def test_format_memory_files_contains_instruction_prompt(tmp_path: Path):
    f1 = tmp_path / "x.md"
    f1.write_text("# user rules", encoding="utf-8")
    info = MemoryFileInfo(
        path=f1.resolve(),
        type="Project",
        content="# user rules",
    )
    out = format_memory_files([info])
    assert MEMORY_INSTRUCTION_PROMPT in out
    assert "x.md" in out
    assert "user rules" in out
    assert "project instructions" in out  # type label


def test_format_memory_files_empty():
    assert format_memory_files([]) == ""


def test_memory_loader_local_overrides_project(tmp_path: Path):
    # 项目 CLAUDE.md
    (tmp_path / "CLAUDE.md").write_text("project-rule", encoding="utf-8")
    # 本地私有 CLAUDE.local.md(优先级最高)
    (tmp_path / "CLAUDE.local.md").write_text("local-rule", encoding="utf-8")

    loader = MemoryLoader(
        cwd=tmp_path,
        include_managed=False,
        include_user=False,
    )
    files = asyncio.run(loader.get_memory_files())
    types = [f.type for f in files]
    contents = [f.content for f in files]
    # Project 优先级低,Local 排在末尾(数组靠后 = 模型更重视)
    assert "Project" in types
    assert "Local" in types
    project_idx = types.index("Project")
    local_idx = types.index("Local")
    assert local_idx > project_idx
    assert "project-rule" in contents
    assert "local-rule" in contents


def test_memory_loader_memoize_caches(tmp_path: Path):
    md = tmp_path / "CLAUDE.md"
    md.write_text("v1", encoding="utf-8")
    loader = MemoryLoader(
        cwd=tmp_path, include_managed=False, include_user=False,
    )

    async def call_twice():
        a = await loader.get_memory_files()
        # 修改文件 -> 由于 memoize,二次调用应仍返回 v1
        md.write_text("v2", encoding="utf-8")
        b = await loader.get_memory_files()
        return a, b

    a, b = asyncio.run(call_twice())
    assert a == b
    # reset_cache 后才能看到 v2
    loader.reset_cache(reason="test")
    c = asyncio.run(loader.get_memory_files())
    assert any("v2" in f.content for f in c)
