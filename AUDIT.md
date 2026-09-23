# Workspace Audit — imp (2026-09-01)

## 1. Executive summary

`imp` is a small (~1,900 lines) terminal coding assistant that drives OpenAI-Responses-API-compatible models through a ReAct loop with seven tools, a workspace-sandboxed filesystem, an SSRF-guarded HTTP client, and JSONL session persistence. It is a deliberately minimal, well-layered codebase in visibly good health: `ruff check .` is clean and all 154 tests pass (run under a temporary Python 3.13 venv — the committed `.venv` targets 3.14 with a stale absolute symlink from the provisioning host, so it cannot execute here; this is an environment artifact, not a code defect).

The code's discipline is real: a single model-call site, a single filesystem access path, a single HTTP choke point, and tests that mirror the package. The three findings that matter most:

1. **[major] SSRF validation is TOCTOU-racy (DNS rebinding)** — `imp/adapters/http.py:24` resolves the host for validation, but the actual connection re-resolves DNS independently, so a rebinding hostname passes the check and connects to an internal address, defeating the guard's purpose.
2. **[minor] Failed shell commands are silent** — `imp/tools/shell.py:110` returns `ok=True` regardless of exit code, and `run_shell` is in `QUIET_TOOLS` (`imp/adapters/ui.py:18`), so a failing command renders nothing in the terminal (verified: `exit 3` → `ok=True`).
3. **[minor] `read_file` loads the entire file into memory before slicing** — `imp/adapters/filesystem.py:173` calls `fh.readlines()` with no size cap on a model-chosen path, so one read of a large log can OOM the whole REPL.

## 2. Architecture as found

**Phase 1 summary.** imp is a single-user CLI agent: a prompt in, a ReAct loop out, rendered to the terminal. `cli.py` owns argparse and the REPL; `app.py` is the composition root owning all resource lifetimes; `agent/` is the loop (`agent.py` orchestrates, `model.py` is the only Responses-API call site, `executor.py` runs tool batches with read-concurrent/mutating-sequential scheduling, `context.py` owns messages and a char-based token budget, `prompt.py` assembles the system prompt); `adapters/` holds all I/O (sandboxed filesystem, SSRF-guarded HTTP, session JSONL, terminal UI); `tools/` defines the seven tools behind a `Tool` ABC. Complexity concentrates in `adapters/filesystem.py` (250 lines covering text-sniffing, frontmatter parsing, sandboxing, and diffing), `adapters/ui.py` (`render_event` dispatch), and `agent/executor.py`'s three-phase batch loop.

**Entry points.**
- `imp = "imp.cli:main"` console script (`pyproject.toml:22`) and `python -m imp.cli` (`Dockerfile:19`) → `main()` → `Config.from_env` → `asyncio.run(_run)` → `UIAdapter` + `build_agent()` → `repl()`.
- `pytest` (`pyproject.toml:37-39`, `asyncio_mode = "auto"`) → `tests/` mirroring the package.
- `.github/workflows/ci.yml` runs `uv sync --frozen`, `ruff check`, `pytest` on push/PR.

**Dependency flow.** `events.py` sits at the bottom, dependency-free (its `ToolResult` import is `TYPE_CHECKING`-only, `imp/events.py:7-8`). `entities.py` imports only stdlib. `agent/` → `entities`, `events`, `config`, `tools`, `adapters` (SessionWriter via `context.py`), `openai`. `tools/` → `adapters`, `config`. `cli.py`/`app.py` compose everything. Layering matches the AGENTS.md contract; no cycles found (`events.py` is the documented cycle-breaker).

**External boundaries.** filesystem via `FileSystemAdapter` only (resolve+`relative_to` sandbox, skip-list); network via `HttpClient` (client-level `redirect_guard` event hook, byte cap) and `AsyncOpenAI`; subprocess via `asyncio.create_subprocess_shell` (temp files, new session, `killpg`); persistence via append-only `SessionWriter` with degrade-not-die error handling.

## 3. Findings

### [MAJOR] SSRF guard is TOCTOU-racy against DNS rebinding — Bugs & logic holes (security)
- **Where:** `imp/adapters/http.py:20-27` and `imp/adapters/http.py:62-68`
- **Evidence:**
  ```python
  infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
  ...
  addrs = {ipaddress.ip_address(i[4][0]) for i in infos}
  ```
  followed by a separate connection that resolves DNS again:
  ```python
  async with self.client.stream("GET", url, ...) as response:
  ```
- **Problem:** `validate_url` resolves the hostname for its private/reserved-IP check, then httpx performs its own independent resolution when opening the connection. An attacker-controlled hostname (the model fetches model-chosen URLs, so this is reachable via prompt content) can answer the first lookup with a public IP and the second with `127.0.0.1`/RFC1918/link-local, passing validation while connecting internally. `redirect_guard` (`http.py:39-41`) re-validates every hop but each validation carries the same race. The guard exists precisely to block internal fetches, so this is a bypass of its core purpose.
- **Minimal fix (ponytail):** no one-liner closes a rebinding race; the lazy honest step is to name the ceiling where the check lives (a `# ponytail:`-style comment at `http.py:24` plus a README line: "internal addresses refused best-effort; DNS rebinding not prevented"). The real fix — resolve once and pin the connection to the validated address (custom transport) — should be scheduled only when fetching untrusted-hostnames-for-attacker-value becomes a real workload.

### [MINOR] `run_shell` reports `ok=True` for failed commands; combined with `QUIET_TOOLS`, failures are invisible — Bugs & logic holes (silent failure)
- **Where:** `imp/tools/shell.py:110`, `imp/adapters/ui.py:18`, `imp/adapters/ui.py:178-179`
- **Evidence:**
  ```python
  return ToolResult(ok=True, content=result)   # shell.py — unconditional
  QUIET_TOOLS = {"read_file", "ask", "list_dir", "web_fetch", "web_search", "run_shell"}
  ...
  if quiet and result.ok:
      return  # reads/questions: result content is noise in the UI
  ```
- **Problem:** verified empirically — `execute(command="exit 3")` returns `ok=True` with `"exit_code": 3`. Since `run_shell` is quiet, the UI prints the `[run_shell]` header and then nothing: no output, no `← failed` marker. The model still sees `exit_code` in the JSON and can adapt, but the human at the terminal gets zero signal that a command failed, and `ToolResult.ok` loses its codebase-wide meaning ("succeeded" per `fs.py` approvals/errors) to mean "process ran". Contrast `shell.py:95-98`, where timeouts correctly return `ok=False`.
- **Minimal fix (ponytail):** one line at the root: `return ToolResult(ok=proc.returncode == 0, content=result)`. The UI then renders `← failed` in red via the existing path (`ui.py:176-180`). Add one test asserting `execute(command="false").ok is False` — the suite currently covers only the exit-0 path (`tests/test_tools.py:125-131`).

### [MINOR] `read_file` loads whole files into memory before slicing — Bugs & logic holes (resource)
- **Where:** `imp/adapters/filesystem.py:172-178`; exposed uncapped at `imp/tools/fs.py:57-63`
- **Evidence:**
  ```python
  with file_path.open("r", encoding="utf-8") as fh:
      lines = fh.readlines()

  total = len(lines)
  start = max(1, start_line)
  end = max(end_line, 0) if end_line is not None else None
  selection = lines[start - 1 : end]
  ```
- **Problem:** the sandbox applies no size limit, `read_file` accepts any workspace path, and the executor's 100k-char truncation (`executor.py:88-96`) only happens *after* the full read. A model-directed `read_file` on a multi-gigabyte log loads it entirely into RAM and can OOM-kill the REPL. Paging (`start_line`/`end_line`) bounds the *output*, not the *read*.
- **Minimal fix (ponytail):** guard at the single access point: in `read_text_file`, `if p.stat().st_size > <cap>` raise `ValueError("file too large; read a range with start_line/end_line")`, and build `selection` with `itertools.islice(fh, start - 1, end)` for ranged reads. A few lines, one place, all callers covered.

### [MINOR] `web_fetch` validates the same URL twice per request — Overcomplication (DRY)
- **Where:** `imp/tools/fetch.py:56`, with the always-on hook at `imp/adapters/http.py:47-52`
- **Evidence:**
  ```python
  async def execute(self, url: str) -> ToolResult:
      await validate_url(url)
  ```
  while every request through the client already fires:
  ```python
  event_hooks={"request": [redirect_guard]},   # "re-run SSRF validation on every redirect hop"
  ```
- **Problem:** httpx request event hooks run on the initial request *and* every redirect, so the first `validate_url` call in `WebFetch.execute` is a duplicate of what the hook does milliseconds later — costing a redundant DNS resolution per fetch and duplicating the knowledge "URLs must be validated" in two layers. The direct `tools/ → adapters.http.validate_url` import also reaches past the `HttpClient` interface into adapter internals.
- **Minimal fix (ponytail):** delete `await validate_url(url)` at `fetch.py:56` (and its import at `fetch.py:9`). The hook fires before the connection and raises the same `ValueError`, which `execute_call` converts to the same tool-error text. This is *not* a request to weaken trust-boundary validation — the hook keeps validating every request and every hop; the pre-check is the redundant copy.

### [MINOR] Executor assumes tool-call IDs are unique; a duplicate crashes the whole REPL — Bugs & logic holes (unhandled path)
- **Where:** `imp/agent/executor.py:61` and `imp/agent/executor.py:92`; guard gap at `imp/agent/agent.py:46-56`
- **Evidence:**
  ```python
  results: dict[str, ToolResult] = {}
  ...
  results[call.call_id].content,
  ```
  while `run_turn` wraps only the model call:
  ```python
  try:
      reply = await call_model(...)
  except Exception as e:
      ... yield ERROR ... return
  ```
- **Problem:** nothing catches exceptions from `execute_tool_batch`, and the `results` dict is keyed by `call_id`. A provider emitting two calls with the same `call_id` (plausible for the local/OpenRouter providers imp explicitly targets, `README.md:8`) makes `results[call.call_id]` raise `KeyError` — or silently overwrite one result at `executor.py:64`. Either way the exception escapes `run_turn`, `repl`, and `asyncio.run` (only `KeyboardInterrupt` is caught, `cli.py:62-63`), killing the session with a traceback instead of an ERROR event.
- **Minimal fix (ponytail):** key `results` by object identity — `results[id(call)] = result` at `executor.py:64` and `results[id(call)].content` at `executor.py:92` (3-line diff) — making duplicate IDs harmless; results still append in call order.

### [MINOR] Documentation drift: `httpx` vs `httpx2`, and the quiet-tools list — SOLID / clean code (DRY)
- **Where:** `README.md:6`, `AGENTS.md:7`, `AGENTS.md:20` vs `pyproject.toml:14`, `imp/adapters/http.py:10`; `README.md:67-68` vs `imp/adapters/ui.py:18`
- **Evidence:** `README.md:6`: "five dependencies (`openai`, `httpx`, `lxml`, `rich`, `prompt_toolkit`)" and `AGENTS.md:20`: "`http.py` (shared `httpx.AsyncClient` wrapper…)" — but the locked, imported package is `httpx2` (`pyproject.toml:14` `"httpx2>=2.12.0"`, `http.py:10` `import httpx2 as httpx`). Likewise `README.md:67-68` lists quiet tools as "`read_file`/`list_dir`/`ask`/`web_fetch`", omitting `web_search` and `run_shell` from `QUIET_TOOLS`.
- **Problem:** a contributor following the docs installs/imports `httpx` and breaks; a user is never told that shell output (including failures, per the finding above) is never displayed. Duplicated knowledge (dependency name, UI behavior) that has already drifted.
- **Minimal fix (ponytail):** four one-line doc edits: `httpx` → `httpx2` in `README.md:6`, `AGENTS.md:7`, `AGENTS.md:20`, and add `run_shell`/`web_search` to the quiet list in `README.md:67-68`.

### [NIT] Skills flow through untyped `list[tuple]` with `len()` branching — SOLID / clean code
- **Where:** `imp/adapters/filesystem.py:137`, `imp/agent/prompt.py:57-64`
- **Evidence:**
  ```python
  def list_skills(self) -> list[tuple]:
  ...
  def _format_skills(skills: list[tuple]) -> str:
      for s in skills:
          if len(s) == 1:
              lines.append(f"- {s[0]}")
          elif len(s) == 2:
  ```
- **Problem:** `list[tuple]` documents nothing; the 1-vs-2-length branching silently drops any tuple of another length, and a reader must trace `parse_skill_frontmatter` to learn the shape is `(name, description)`.
- **Minimal fix (ponytail):** one 3-line `NamedTuple` (`class Skill(NamedTuple): name: str; description: str | None = None`) returned by `list_skills` and consumed by `_format_skills`; deletes the `len()` branches.

### Leaky abstractions
None found. (`tools/ → adapters` coupling exists by design — `Tool`'s constructor injects `FileSystemAdapter`/`HttpClient`; the one soft spot, `fetch.py` importing `validate_url`, is covered by the duplicate-validation finding above.)

### Clean architecture violations
None found. The layering is deliberately thin and consistently applied: domain objects in `entities.py` import only stdlib, `events.py` is runtime-dependency-free, `agent/context.py`'s import of the concrete `SessionWriter` matches the project's stated pragmatism, and composition/lifetimes live in `app.py`.

## 4. What is already good

- **The sandbox is done right once:** `FileSystemAdapter.resolve_path` (`filesystem.py:79-95`) resolves symlinks *then* checks `relative_to`, so traversal and symlink escapes are rejected by construction; skip-list enforcement and the binary-sniffing edge case (multi-byte char straddling the 4096-byte probe, `filesystem.py:19-24`) are handled and regression-tested.
- **SSRF choke point placement:** one client-level `redirect_guard` event hook (`http.py:44-52`) validates the initial request and every redirect hop for all consumers — the right single place, plus a streamed byte cap so response size can't run away.
- **Shell hygiene:** temp files instead of pipes to dodge asyncio's pipe-gated `wait()` wedge, `start_new_session=True` + `killpg` so grandchildren die with the command, and tail-capped output capture (`shell.py:68-93`) — each with a comment naming the exact failure it prevents.
- **Context ownership:** stateless Responses-API usage with verbatim reasoning-item replay (`model.py:55-67`, `entities.py:45-65`), truncation applied at the single context-append point, and a turn-abort budget check before and after tool batches.
- **Graceful degradation:** `SessionWriter` disables itself with a stderr warning instead of crashing when persistence is unavailable (`session.py:50-57`).
- **Discipline:** `ruff` clean, 154/154 tests passing, test constants imported from source (never hardcoded), and every deliberate shortcut carries a `lazy:`/rationale comment — the codebase reads exactly like its AGENTS.md describes.

## 5. Recommended action order

1. **One line:** `shell.py:110` → `ok=proc.returncode == 0`, plus one test for the non-zero path — makes shell failures visible and restores `ToolResult.ok` semantics. *(Finding: silent shell failures)*
2. **A few lines:** size guard + `islice` in `read_text_file` — removes the one realistic OOM path. *(Finding: unbounded read)*
3. **One comment + one README line:** document the DNS-rebinding ceiling on `validate_url`; schedule the pinned-IP connect only if/when it matters. *(Finding: SSRF TOCTOU)*
4. **Three lines:** key executor `results` by `id(call)` — duplicate call IDs can no longer crash the REPL. *(Finding: call_id collision)*
5. **Two-line deletion:** drop the redundant `validate_url` pre-check in `WebFetch.execute`. *(Finding: double validation)*
6. **Four doc lines:** `httpx` → `httpx2` (README, AGENTS ×2) and complete the quiet-tools list. *(Finding: doc drift)*
7. **Optional nit:** `Skill` NamedTuple to replace `list[tuple]`. *(Finding: untyped skills)*
