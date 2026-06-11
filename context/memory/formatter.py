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
) -> str:
    """把多个 MemoryFileInfo 拼成 memory section 注入文本。

    files 为空返回空串(memory section 会被过滤掉)。
    """
    if not files and not knowledge_context.strip():
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
    return "\n".join(chunks)


__all__ = ["MEMORY_INSTRUCTION_PROMPT", "format_memory_files"]
