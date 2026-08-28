# imp

A small OpenAI-compatible CLI coding assistant in pure Python. Useful agent, total
control over the context window, and a codebase you can read in one sitting.

- **Small**: ~1,650 lines, five dependencies (`openai`, `httpx`, `lxml`, `rich`, `prompt_toolkit`).
- **Provider-agnostic**: anything speaking the OpenAI chat-completions API.

Project goals:
* explore how coding harnesses work
* have a usable assistant with full context control (for use with local llms)
* readability and extensibility

## Quickstart

Requires Python 3.13+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                         # install deps into .venv
export OPENAI_API_KEY=sk-...    # your provider key
uv run imp                      # start the REPL (python -m imp.cli also works)
```

Any OpenAI-compatible endpoint works:

```bash
export OPENAI_BASE_URL=https://api.groq.com/openai/v1
export OPENAI_MODEL=llama-3.3-70b-versatile
```

Useful with OpenRouter:

```bash
export OPENAI_BASE_URL=https://openrouter.ai/api/v1/
export OPENAI_API_KEY=sk-or...
export OPENAI_MODEL=minimax/minimax-m3:free
```

### Docker

The secure way to use this agent is by sandboxing it with Docker:

```bash
docker build -t imp .
docker run -it --rm \
  -v "$PWD:/workspace" \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -e OPENAI_API_KEY=sk-... \
  imp
```

`--user` runs the container as your host user so files the agent writes in the
mounted workspace are owned by you; `-e HOME=/tmp` gives that user a writable
home. imp writes nothing outside the workspace itself. CLI flags go after
the image name (`imp -y` to auto-approve tool calls).

### Usage

A banner shows the model, workspace, and key hints, then you get a `>` prompt.
Arrow keys edit and recall input; end a line with `\` to continue onto the
next line; pasted multi-line text is kept verbatim (Enter sends, Esc+Enter
inserts a newline). While the model thinks a spinner runs; responses render as
plain markdown (no borders — terminal text stays copy-friendly), file edits as
diffs, tool calls as `[tool_name] args`, and quiet read tools
(`read_file`/`list_dir`/`ask`/`web_fetch`) print nothing on success. Context
usage prints once at the end of each turn; tool output truncates to
`IMP_MAX_TOOL_DISPLAY_LINES` lines. Mutating tools (`write_file`,
`str_replace`, `run_shell`) ask `Allow ... (y/n)?` first, or pass `-y` to
auto-approve.

Plain `exit`/`quit` or Ctrl-D leaves; Ctrl-C clears the prompt or interrupts a
running turn.

## How it works

A single ReAct loop per turn:

```
user prompt ─▶ model ─▶ tool calls ─▶ tool results ─▶ model ─▶ ... ─▶ final text
```

- **Scheduling**: within one batch of tool calls, read-only tools run
  concurrently; mutating tools run sequentially. Results always append in
  tool-call order, so sessions replay deterministically.
- **Context budget**: tokens are estimated (`len(json)/4 + 20` per message);
  the turn aborts when usage crosses 95% of the budget, and oversized tool
  output is truncated before entering the context.
- **System prompt**: assembled at startup from the base prompt, environment,
  tool list and per-tool instructions, discovered skills, and
  `AGENTS.md`/`CLAUDE.md` if present.
- **Persistence**: every message is appended to
  `.imp/sessions/<timestamp>-<id>.jsonl` (persistence only — the agent
  always runs from the in-memory context).

### Tools

| Tool | Mutating | Purpose |
|---|---|---|
| `list_dir`, `read_file` | no | Inspect the workspace (line ranges, depth levels) |
| `write_file`, `str_replace` | yes | Create/overwrite files; replace a unique string, returns a diff |
| `run_shell` | yes | Shell command at the workspace root, timeout-capped |
| `ask` | no | Ask the user a blocking question |
| `web_fetch` | no | Fetch a URL; scripts/styles/comments stripped; internal addresses refused |
| `web_search` | no | Brave Search — only registered if `BRAVE_API_KEY` is set |

### Skills

Drop a directory under `<workspace>/.imp/skills/<name>/SKILL.md` with YAML frontmatter:

```markdown
---
name: my-skill
description: When this matches the task, load me first.
---
Detailed instructions...
```

The agent sees only name + description in the system prompt and reads the full
file lazily when relevant.

## Configuration

Environment only (see `.env.example`); no config files to parse.

| Variable | Default | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | — | **Required.** Provider API key. |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model name. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint. |
| `BRAVE_API_KEY` | unset | Enables the `web_search` tool. |
| `IMP_WORKSPACE` | `.` | Sandbox root (absolute or relative). |
| `IMP_MAX_ITERATIONS` | `50` | Max ReAct iterations per turn. |
| `IMP_MAX_CONTEXT` | `128000` | Estimated context budget in tokens. |
| `IMP_COMMAND_TIMEOUT` | `180` | Shell command timeout (s). |
| `IMP_NETWORK_TIMEOUT` | `360` | HTTP/model timeout (s). |
| `IMP_MAX_TOOL_OUTPUT` | `100000` | Tool output cap in chars before truncation. |
| `IMP_MAX_TOOL_DISPLAY_LINES` | `10` | Max tool output lines shown in the UI. |
| `IMP_MAX_HTTP_BYTES` | `10000000` | HTTP response cap in bytes. |
| `IMP_AUTO_APPROVE` | unset | Skip approvals (`1/true/yes/on/y`). Same as `-y`. |

## Extending

**Add a tool** — subclass `Tool` in `imp/tools/`, then instantiate it in
`build_tools()` (`tools/__init__.py`). Dependencies (`config`, `fs`,
`prompt_user`, `http`) arrive via the base-class constructor. Set
`mutating = True` if it changes state; return
`ToolResult(ok, content, diff=None)` with error text the model can act on —
never raise across the tool boundary.

**Add a skill** — no code: just a directory with `SKILL.md` (see above)
within the workspace.

**Add a setting** — env-only: add the field to `Config` and read it in
`Config.from_env` (`config.py`).

### Project layout

```
imp/
├── cli.py              # argparse, REPL loop
├── app.py              # build_agent(): composition root, resource lifetimes
├── config.py           # Config dataclass — all env vars and defaults
├── entities.py         # frozen message dataclasses, serialize()/parse()
├── agent/
│   ├── __init__.py     # Agent.run_turn(): the ReAct loop, event stream
│   ├── context.py      # message list + token estimation/limits
│   └── prompt.py       # system prompt assembly
├── adapters/           # filesystem (sandbox), http, session, ui
└── tools/              # Tool ABC + build_tools + shell/fs/ask/fetch/brave_search
```

`AGENTS.md` documents the same layout for agents (including this one) and is
injected into every system prompt.

## Development

```bash
uv run ruff check .                     # lint (clean — keep it clean)
uv run python -m pytest                 # test suite
```

## References

- [OpenAI guide on function calling](https://developers.openai.com/api/docs/guides/function-calling?api-mode=chat)
- [nano — one-file coding assistant](https://github.com/pnegahdar/nano)

## Potential features

* Restoring from a session file name with a cli argument
* Replace `_token_formula` with tiktoken or other token counting method