"""把 MemoryFileInfo 列表组装成 system prompt 注入文本。

对应 claude-code 中 getClaudeMds(也叫 loadMemoryPrompt 的内部分支)。

注入文本结构:
    {MEMORY_INSTRUCTION_PROMPT}

    Contents of {path} (project instructions, checked into the codebase):

    {content}

    Contents of {next_path} (...):
    ...

每段之间空行分隔,模型容易识别每段的来源。来源标签按 type 区分:
- Managed: "managed instructions"
- User:    "user instructions"
- Project: "project instructions, checked into the codebase"
- Local:   "local user instructions, NOT checked in"
"""

from __future__ import annotations

from typing import Sequence

from .types import MemoryFileInfo, MemoryType


MEMORY_INSTRUCTION_PROMPT = (
    "Codebase and user instructions are shown below. Be sure to adhere to "
    "these instructions. IMPORTANT: These instructions OVERRIDE any default "
    "behavior and you MUST follow them exactly as written."
)


_TYPE_LABEL: dict[MemoryType, str] = {
    "Managed": "managed instructions",
    "User": "user instructions",
    "Global": "global memory and user profile",
    "Project": "project instructions, checked into the codebase",
    "ShortTerm": "short-term project memory",
    "Local": "local user instructions, NOT checked in",
    "Knowledge": "retrieved structured knowledge",
}


def format_memory_files(
    files: Sequence[MemoryFileInfo],
    *,
    knowledge_context: str = "",
    omitted: Sequence[MemoryFileInfo] = (),
    truncated_paths: Sequence[str] = (),
) -> str:
    """把多个 MemoryFileInfo 拼成 memory section 注入文本。

    files 为空且无 knowledge/manifest 时返回空串。
    omitted/truncated 生成模型可见 manifest，禁止静默丢弃。
    """
    if not files and not knowledge_context.strip() and not omitted and not truncated_paths:
        return ""
    chunks: list[str] = [MEMORY_INSTRUCTION_PROMPT]
    for f in files:
        label = _TYPE_LABEL.get(f.type, "instructions")
        chunks.append(
            f"\nContents of {f.path} ({label}):\n\n{f.content}"
        )
    if knowledge_context.strip():
        chunks.append(
            "\nRetrieved knowledge context (structured knowledge base / RAG):\n\n"
            + knowledge_context.strip()
        )
    manifest_lines: list[str] = []
    for f in omitted:
        manifest_lines.append(f"- omitted ({f.type}): {f.path}")
    for path in truncated_paths:
        manifest_lines.append(f"- truncated preview: {path}")
    if manifest_lines:
        chunks.append(
            "\nMemory budget manifest (files not fully injected):\n"
            + "\n".join(manifest_lines)
        )
    return "\n".join(chunks)


__all__ = ["MEMORY_INSTRUCTION_PROMPT", "format_memory_files", "_TYPE_LABEL"]
