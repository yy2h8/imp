# AGENTS.md

imp is a minimal OpenAI-compatible CLI coding assistant in pure Python. The goal is a useful coding agent with total control over its context window — readability and extensibility over features. Every decision here reflects that; when in doubt, choose the smaller change.

## Architecture (WHAT)

Python 3.13, managed with uv. Runtime dependencies kept deliberately tiny: `openai`, `httpx2`, `lxml[html-clean]`, `rich`, `prompt_toolkit` (dev: `ruff`, `pytest`, `pytest-asyncio`). Package lives at repo root in `imp/` (flat layout, no src wrapper; hatchling build with an `imp` = `imp.cli:main` script entry).

- `cli.py` — entry point; argparse, config error handling, `asyncio.run`, and the REPL (`repl`).
- `app.py` — `build_agent()`: async composition root (adapters, tools, system prompt, Agent); creates the `AsyncOpenAI` client directly and owns its lifetime along with HttpClient/SessionWriter; provides the locked async `prompt_user` (delegates to `UIAdapter.ask`) so concurrent asks/approvals never interleave.
- `config.py` — `Config` dataclass built from env vars; the source of truth for all env var names and defaults (incl. `IMP_REASONING_EFFORT`).
- `entities.py` — frozen message dataclasses (`ConversationMessage` hierarchy, `ToolCall`) with `serialize()`/`parse()`; pure domain, no SDK imports. Item-shaped for the Responses API; `ReasoningMessage` holds the raw item verbatim for stateless replay.
- `events.py` — `EventType`/`AgentEvent`: the agent-loop event contract the UI renders; dependency-free at runtime (must not import `tools`/`adapters`/`agent` — that would recreate a cycle `agent → tools → adapters → ui → agent`).
- `agent/` — the ReAct loop, one responsibility per module; `__init__.py` re-exports the public API (`Agent`, `AgentEvent`, `Context`, `EventType`, `build_system_prompt`).
  - `agent.py` — `Agent.run_turn()`: async ReAct loop orchestration (iterations, token limits, event flow); model calls and tool batches are delegated out.
  - `model.py` — `call_model()`: the only Responses-API call site — builds the stateless request (`store: false`; reasoning keys only when `IMP_REASONING_EFFORT` is set; reasoning items replay only when they carry `encrypted_content`) and parses output items into a `ModelReply`.
  - `executor.py` — `execute_tool_batch()`: per batch, read-only tools run concurrently and mutating tools run sequentially (`Tool.mutating`), results append in tool-call order (deterministic sessions); also truncates oversized tool output.
  - `context.py` — `Context`: message list plus char-based token estimation and limit checks.
  - `prompt.py` — assembles the system prompt (base prompt, environment, tools + per-tool instructions, skills, project context).
- `adapters/` — infrastructure behind thin interfaces. `filesystem.py` (`FileSystemAdapter`: the only filesystem access path for model-requested files; sandboxed to the workspace — no path escapes, skips `.git`/`__pycache__`/`.env`; also discovers skills and injects this file into the system prompt at runtime), `http.py` (shared `httpx2.AsyncClient` wrapper with SSRF validation on every request/redirect), `session.py` (`SessionWriter`: persistence to `.imp/sessions/*.jsonl`; appends directly — infrastructure, not sandboxed, gitignored), `ui.py` (`UIAdapter`: prompt_toolkit input + rich output — banner, markdown/diff rendering, spinner, per-turn context usage, line truncation; owns all terminal interaction).
- `tools/` — `Tool` ABC (`base.py`: name/description/parameters/required/instructions as ClassVars, `mutating` flags tools that serialize within a batch; constructor injects `config`/`fs`/`prompt_user`/`http`; async `execute()` returns `ToolResult(ok, content, diff=None)`); `build_tools()` in `__init__.py` instantiates and registers them. Modules: `shell.py` (run_shell), `fs.py` (list_dir/read_file/write_file/str_replace), `ask.py` (ask), `fetch.py` (web_fetch), `brave_search.py` (web_search, only registered if `BRAVE_API_KEY` is set).
- `.imp/skills/<name>/SKILL.md` — skills with YAML frontmatter (name, description), loaded lazily at runtime.
- `tests/` — pytest suite mirroring the package, one file per module group; constants imported from source, never hardcoded.

Note: this file is also read by imp itself on startup (`FileSystemAdapter.CONTEXT_FILES`) and goes into its system prompt every session — keep it lean and current.

## Commands (HOW)

```bash
uv sync                                  # install deps into .venv
uv run imp --help                       # run (installed by uv sync; python -m imp.cli also works)
uv run ruff check .                      # lint (clean — keep it clean; BLE001 ignored by design)
uv run python -m pytest                  # test suite (asyncio_mode=auto in pyproject)
```

## Conventions

- Match the existing code: `@dataclass(slots=True)` (frozen for immutable values), `from __future__ import annotations`, ABC + ClassVar for tool definitions.
- All filesystem access goes through `FileSystemAdapter`; never bypass the sandbox.
- Tools return `ToolResult(ok, content, diff=None)` with error text the model can act on — no exceptions across the tool boundary.
- Async end to end: keep blocking I/O off the event loop — native async APIs or `asyncio.to_thread`.
- New tools: subclass `Tool`, instantiate it in `build_tools()` (`tools/__init__.py`).
- Runtime config is env-only; new settings go through `Config.from_env`.

## Working principles

- Simplicity first: minimum code that solves the problem, nothing speculative.
- Surgical changes: touch only what the task requires, match existing style, and make every changed line trace to the request.
- Verify before claiming done: at minimum `pytest` plus `ruff` on changed files. State assumptions instead of guessing; ask when multiple interpretations exist.

## Roadmap

Check `README.md` todos before planning work.
