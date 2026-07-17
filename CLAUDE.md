# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working directory

The actual Python project lives in `cb-agent/` (this directory). Sibling folders under `..` (`graph_data/`, `knowledge_base/`, `memory_data/`, `zvec_data/`, `source/`, `外部代码/`) are runtime data dirs and external reference code, not part of the package — don't import or modify them.

The shared venv lives one level up at `../venv`. Always run Python through it:

```bash
# from cb-agent/
cbagent                                      # Launch OTUI from the current workspace
```

`pyproject.toml` excludes `test*`, `venv*`, `外部代码*`, `note*` from the installed package — when adding new top-level dirs, decide whether they should ship with the package and update the exclude list accordingly.

## Common commands

Run all from inside `cb-agent/`:

```bash
# Install (two tiers — see README "依赖按场景分两档")
pip install -r requirements.txt && pip install -e .          # core backend + OTUI gateway
pip install -r requirements-full.txt && pip install -e ".[full]"  # full (RAG, vector/graph stores, PDF, web search)

# OTUI entrypoint — wires the JSON-RPC backend automatically
cbagent

# Pytest-style unit tests (currently only context module)
../venv/Scripts/python.exe -m pytest test/ -q

# Standalone test scripts (NOT pytest — each has its own __main__ and prints results)
../venv/Scripts/python.exe test/test_context_builder.py
../venv/Scripts/python.exe test/test_memory_operations.py
../venv/Scripts/python.exe test/test_rag_operations.py
../venv/Scripts/python.exe test/test_skills.py
../venv/Scripts/python.exe test_vector_store.py     # at repo root, not test/
../venv/Scripts/python.exe test_graph_store.py
../venv/Scripts/python.exe test.py                  # ad-hoc end-to-end smoke
```

OTUI slash commands are defined in `ui-otui/src/commands.ts`; `run_agent.py` only exposes backend transports.

## Configuration

- Copy `.env.example` to `.env` and fill at minimum `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL_ID`. `.env` is gitignored.
- The model id in `LLM_MODEL_ID` **must exist as a key in [`constant/llm/constant_llm.py`](constant/llm/constant_llm.py)** — `CbAgentsLLM._is_able_Function_Calling` looks it up there to decide whether to use the function-calling code path. Adding a new model means adding an entry to `ConstantLLM.llm_dict` with `is_tool` / `is_reasoning` / `max_tokens` / `image_ability` flags.
- `mcp.json` references env vars via `${VAR}` placeholders — keys live in `.env`, never inline.

## Architecture

### One-liner

`run_agent.py` is the integration point. Everything else is a library module composed by it:

```
user input → ContextBuilder (GSSC) → CbAgentsLLM.think (streaming FC)
                  ▲                        │
                  │                        ▼
       Memory + RAG + history       ToolRegistry (native + MCP) + Skills
```

### The five subsystems and how they wire together

1. **`agent/cb_agents.py` — `CbAgentsLLM`**
   OpenAI-compatible client. The interesting method is `_think_with_Function_Calling`: under `stream=True` it accumulates `delta.tool_calls` **by `index`** (both `name` and `arguments` arrive as multi-chunk fragments), and surfaces `delta.reasoning_content` separately so OTUI can render the reasoning block. If you touch streaming, preserve this fragment-reassembly logic — DeepSeek thinking models depend on it.

2. **`context/builder.py` — `ContextBuilder` (GSSC pipeline)**
   Gather → Select (relevance + recency + MMR) → Structure (priority-bucketed sections `[Role & Policies]` / `[State]` / `[Evidence]` / `[Context]`) → Compress (drop whole packets, never split sections). Has both sync `build()` and async `abuild()` (the latter uses `asyncio.gather + to_thread` to parallelize memory/RAG retrieval). Read [`context/README.md`](context/README.md) before changing priorities or section templates.

3. **`tools/` — Tool abstraction with two sources**
   - `tools/tool.py` defines the `Tool` ABC; `tools/toolRegistry.py` is the registry.
   - Native tools live in `tools/tools/` (memory, rag, search, todo, bash, file tools, etc.). Each subclasses `Tool` and implements `get_parameters` / `validate_parameters` / `run`.
   - MCP tools come in via `tools/mcp_tools/mcptools_add.py:load_mcp_tools` — it reads `mcp.json`, launches each server, and **auto-expands every server's tool list into individual `MCPWrappedTool` instances** registered alongside native tools. The agent never sees the difference.

4. **`skills/` — Prompt-as-Capability**
   Skills are Markdown files with YAML frontmatter under `.cbagent/skills/<name>/SKILL.md`. `SkillManager` discovers them and renders only a budgeted name/description/file index into context. Explicit `$skill` mentions inject the markdown body for the current turn; implicit use means the LLM reads the listed `SKILL.md` with `file_read`. Bundled scripts are run through `bash`, with script hits recorded after the fact.

5. **`memory/` — Three-layer memory + multimodal RAG**
   - `memory/types/` — `episodic`, `semantic`, `working`, `perceptual` memory classes.
   - `memory/storage/` — pluggable backends: vector (`zvec` local | `qdrant`), graph (`sqlite` | `neo4j`). Backend chosen by `VECTOR_STORE_TYPE` / `GRAPH_STORE_TYPE` env vars. Managers (`vector_store_manager.py`, `graph_store_manager.py`) hide the choice from callers.
   - `memory/rag/pipeline.py` — text/image/audio unified pipeline; OCR/ASR convert non-text to text before embedding.
   - The `memory` tool in the LLM's tool list auto-routes reads/writes to the right memory type — callers don't pick episodic/semantic/working manually.

### The tool-calling loop (`run_agent.py:AgentRunner`)

Each round calls `llm.think(messages, tools=registry.openai_schema)`, appends the assistant message and tool results, then repeats until the model returns no more `tool_calls`. Hitting `MAX_TOOL_ROUNDS` is a hard stop, not a warning.

### Tool vs MCP vs Skill — the boundary

| | Tool | MCP | Skill |
|---|---|---|---|
| What it is | Atomic Python function | External process speaking MCP | Markdown workflow + optional scripts |
| How LLM uses it | OpenAI function call | OpenAI function call (wrapped) | Reads SKILL.md when the overview or `$skill` mention says it applies, then uses normal tools |
| When to add one | New atomic capability in-process | Reusing an existing MCP server | Multi-step workflow you want the LLM to follow declaratively |

Don't reach for a Skill when a Tool is enough, and don't reach for a Tool when an MCP server already exposes the capability.

## Conventions specific to this repo

- **Code comments and most docs are in Chinese.** Match that when editing existing files. New module-level docstrings should also be Chinese to stay consistent with `agent/cb_agents.py`, `run_agent.py`, etc.
- **Logging is intentionally quiet at startup** (`run_agent.py` pins `memory*` loggers to `ERROR`). If you add new noisy modules, follow the same pattern instead of bumping the global level.
- **Windows console UTF-8 reconfig** at the top of `run_agent.py` is load-bearing — keep it for any new entrypoints that print emoji or Chinese.
- **`test/` mixes pytest-style and `__main__`-style tests.** The `python -m pytest test/` invocation only picks up the pytest-style ones; the others (and `test_vector_store.py` / `test_graph_store.py` at the repo root) must be run as scripts.

## When you're stuck

- `mcp.json` server failed to start → check the `${VAR}` referenced in its `env` block is set in `.env`; missing keys cause silent skip in `load_mcp_tools`.
- `is_tool` KeyError on startup → `LLM_MODEL_ID` isn't registered in `ConstantLLM.llm_dict`.
- Streaming output looks scrambled → likely something appended to the tool-call accumulator without keying on `index`; re-check `_think_with_Function_Calling`.
- Module docs to consult before deeper changes: [`context/README.md`](context/README.md), [`tools/TOOL_SYSTEM_DESIGN.md`](tools/TOOL_SYSTEM_DESIGN.md), [`skills/SKILLS_GUIDE.md`](skills/SKILLS_GUIDE.md), [`memory/rag/RAG_GUIDE.md`](memory/rag/RAG_GUIDE.md), [`memory/storage/VECTOR_STORE_GUIDE.md`](memory/storage/VECTOR_STORE_GUIDE.md), [`memory/storage/GRAPH_STORE_GUIDE.md`](memory/storage/GRAPH_STORE_GUIDE.md).
