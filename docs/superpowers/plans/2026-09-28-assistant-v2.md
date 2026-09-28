# assistant v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the Telegram assistant on aiogram 3 + APScheduler 3.11 + one SQLite `state.db`, with concurrent jobs, cost tracking, a tool-only turn status, agent memory, and DB-backed transcripts — per the approved spec.

**Architecture:** Phased in-place rewrite on trunk. imp core gains only a `Usage` value on events; the assistant swaps transport (aiogram), scheduling (APScheduler jobstore in `state.db`), and state (aiosqlite repos + a sync `DbSessionWriter`), composed by a rewritten `app.py`/`main.py`. An in-memory `Outbox` defers job-result delivery while an interactive turn is active.

**Tech Stack:** Python 3.12 (uv), aiogram >=3.31, apschedule[sqlalchemy] ~=3.11, aiosqlite, telegramify-markdown >=1.4, pytest/pytest-asyncio (asyncio_mode=auto), ruff.

**Spec:** `docs/superpowers/specs/2026-09-28-assistant-v2-design.md`

## Global Constraints

- imp's three backward-compat seams stay untouched: `build_system_prompt(..., base_prompt=...)`, `FileSystemAdapter(workspace, skills_dir=...)`, `SessionWriter(workspace, sessions_dir=None)`. `DbSessionWriter` duck-types the seam from the assistant side.
- One SQLite DB: `<IMP_HOME>/state.db`, WAL mode, `PRAGMA busy_timeout=30000` on every connection (aiosqlite, sync sqlite3, and APScheduler's engine via `connect_args={"timeout": 30}`).
- Schema migrations: `PRAGMA user_version`; v1 = the initial schema in Task 2.
- No v1-state migration: `state.json` and `jobs/*.json` are ignored, never read.
- Fresh-start constants (copy verbatim): `MAX_MEMORY_KEYS = 200`, `MAX_MEMORY_VALUE_CHARS = 2048`, `MEMORY_DIGEST_CHARS = 1500`, `STATUS_LINES = 8`, `DEFAULT_MAX_CONCURRENT_JOBS = 2`, `misfire_grace_time = 60`, `coalesce = True`, `max_instances = 1`.
- Turn summary formats (copy verbatim): `✓ done · {tools} tools · {seconds} s` plus ` · ${cost:.4f}` only when cost is known; `✗ failed · {tools} tools · {seconds} s`.
- Status headers: `🧠 thinking…` before any tool line, `🧠 working` after the first; overflow line `… +{n} earlier`.
- All new assistant tools subclass `imp.tools.Tool`, return `ToolResult(ok, content)` error text (no exceptions across the boundary), and register in `build_assistant_tools`.
- Conventions: `from __future__ import annotations`, `@dataclass(slots=True)` (frozen for values), `ClassVar` tool definitions, `uv run ruff check .` and `uv run python -m pytest` green after every task.
- Config is env-only; new setting goes through `AssistantConfig.from_env`.

## Review Focus

1. **Duplicate Telegram updates after a crash** (aiogram does not persist offsets; Telegram re-sends unconfirmed updates) — a redelivered message must not enqueue twice. Test in Task 13 (`test_intake.py::test_redelivered_update_not_duplicated`).
2. **Album items straggling** (Telegram sends album photos as separate updates, potentially >1 s apart, interleaved with other messages) — all items land in one queue entry. Test in Task 13 (`test_intake.py::test_album_merge_interleaved`).
3. **m4a sent as playable audio vs document** — `audio` (MIME audio/mp4) and `document` must both save and prompt. Test in Task 13 (`test_uploads.py::test_m4a_audio_and_document`).
4. **Provider response without usage/cost** (non-OpenRouter or API change) — `usage` is `None`, turn summary omits the cost segment, nothing crashes. Test in Task 1 (`test_agent.py::test_model_response_event_without_usage`).
5. **SQLite contention** (three connection families on one file: aiosqlite, sync sqlite3, APScheduler's engine) — WAL + busy_timeout keep writers from raising `database is locked`. Test in Task 2 (`test_db.py::test_concurrent_writers_do_not_lock`).

---

### Task 1: imp — provider usage onto events

**Files:**
- Modify: `imp/events.py`, `imp/agent/model.py`, `imp/agent/agent.py`
- Test: `tests/test_agent.py`

**Interfaces:**
- Produces: `Usage` frozen dataclass in `imp/events.py` — `Usage(input_tokens: int, output_tokens: int, total_tokens: int, cost_usd: float | None)`; `AgentEvent.usage: Usage | None = None`. `ModelReply.usage: Usage | None = None` in `imp/agent/model.py`. Later tasks sum `event.usage` across MODEL_RESPONSE events.

- [ ] **Step 1: Write the failing tests**

In `tests/test_agent.py` (follow its existing fake-client patterns): one test that a fake `responses.create` result whose `usage` carries `input_tokens=10, output_tokens=5, total_tokens=15` and a `cost=0.0123` attribute yields a MODEL_RESPONSE event with `event.usage == Usage(10, 5, 15, 0.0123)`; one test `test_model_response_event_without_usage` — fake result with `usage=None` → `event.usage is None`; one parse-level test asserting a missing/absent `cost` attribute yields `cost_usd=None`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_agent.py -v`
Expected: FAIL — `Usage` not defined / attribute missing.

- [ ] **Step 3: Implement**

`imp/events.py`: add the frozen `Usage` dataclass and the `AgentEvent.usage` field (default `None`) — it must stay runtime-dependency-free. `imp/agent/model.py`: read `response.usage` via `getattr` (OpenRouter adds `cost` beyond the OpenAI schema; use `getattr(usage, "cost", None)`); attach to `ModelReply`. `imp/agent/agent.py`: pass `usage=reply.usage` on the MODEL_RESPONSE event only.

- [ ] **Step 4: Run the full imp suite**

Run: `uv run python -m pytest tests/test_agent.py tests/test_context.py -v && uv run ruff check imp/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add imp/events.py imp/agent/model.py imp/agent/agent.py tests/test_agent.py
git commit -m "feat(imp): surface provider usage and cost on MODEL_RESPONSE events"
```

---

### Task 2: dependencies, config, db schema + kv/queue repos

**Files:**
- Modify: `pyproject.toml`, `assistant/config.py`
- Create: `assistant/db.py`
- Test: `tests/test_db.py` (new), `tests/test_assistant_config.py`

**Interfaces:**
- Produces: `async def open_db(path: Path) -> aiosqlite.Connection` (WAL, busy_timeout, `user_version` migrations, idempotent DDL, indexes `queue(state, id)`, `turns(ts)`, `turns(kind, ts)`).
- Produces: kv — `async def kv_get(conn, key: str) -> str | None`, `async def kv_set(conn, key: str, value: str) -> None`.
- Produces: queue — `async def queue_push(conn, kind: str, payload: str) -> int`; `async def queue_claim_next(conn) -> tuple[int, str, str] | None` (oldest `waiting` → `active`, returns `(id, kind, payload)`); `async def queue_count_waiting(conn) -> int`; `async def queue_finish(conn, row_id: int) -> None` (delete); `async def queue_interrupted(conn) -> list[tuple[int, str, str]]` (all `active` rows, deletes them — callers report, never replay).
- Produces: `AssistantConfig.max_concurrent_jobs: int` from `IMP_MAX_CONCURRENT_JOBS` (int, default `DEFAULT_MAX_CONCURRENT_JOBS = 2`, must be ≥ 1).
- Consumes: Task 8/11/13 use these repos; `STATE_DB_NAME = "state.db"` constant here.

- [ ] **Step 1: Write the failing tests**

`tests/test_db.py` with a tmp-path `open_db`: kv round-trip; `queue_push` twice → `queue_claim_next` returns the first in order and the second next; `queue_count_waiting` counts only `waiting`; `queue_claim_next` then `queue_interrupted` reports exactly that row; `open_db` twice on the same path applies migrations idempotently (`user_version == 1` both times). `test_concurrent_writers_do_not_lock`: 50 interleaved `kv_set` calls from a second `open_db` connection plus sync `sqlite3` writes on the same file — all succeed. Config test: default 2, `IMP_MAX_CONCURRENT_JOBS=5` → 5, `0` → `ValueError`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_db.py tests/test_assistant_config.py -v`
Expected: FAIL — module/config missing.

- [ ] **Step 3: Implement**

`pyproject.toml` dependencies += `aiogram>=3.31`, `apscheduler[sqlalchemy]~=3.11`, `aiosqlite>=0.21`, `telegramify-markdown>=1.4`; run `uv sync`. `assistant/db.py`: DDL for the six spec tables (`kv`, `memory`, `queue`, `turns`, `transcripts`, `jobs_meta`) — `queue.state` ∈ waiting/active/done, `turns` columns per spec (ts TEXT ISO-UTC, cost_usd REAL nullable, ok INTEGER 0/1); `PRAGMA journal_mode=WAL`, `PRAGMA busy_timeout=30000` on connect. Sync sqlite3 connections elsewhere must issue the same pragmas.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_db.py tests/test_assistant_config.py -v && uv run ruff check assistant/db.py`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock assistant/db.py assistant/config.py tests/test_db.py tests/test_assistant_config.py
git commit -m "feat(assistant): state.db schema, kv and queue repositories, new deps"
```

---

### Task 3: db — memory, turns, jobs_meta, transcript-search repos

**Files:**
- Modify: `assistant/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: memory — `async def memory_set(conn, key: str, value: str) -> None` (raises `ValueError` if `len(value) > MAX_MEMORY_VALUE_CHARS` or when inserting the 201st key), `async def memory_delete(conn, key: str) -> bool`, `async def memory_all(conn) -> list[tuple[str, str]]` (key, value; oldest-updated first), `async def memory_digest(conn) -> str` (one `key: value` line per entry, values truncated to 120 chars, newest entries kept, total capped at `MEMORY_DIGEST_CHARS` with a trailing `… +N more memories` line).
- Produces: turns — `async def turn_insert(conn, *, ts: str, kind: str, session_id: str, model: str, in_tokens: int, out_tokens: int, cost_usd: float | None, tools: int, seconds: float, ok: bool) -> None`; `async def turns_report(conn, period: str) -> dict` with keys `turns`, `in_tokens`, `out_tokens`, `cost_usd` — `period` ∈ day/week/month/all (boundaries computed in UTC from `datetime.now(UTC)`).
- Produces: jobs_meta — `async def jobs_meta_upsert(conn, *, schedule_id: str, label: str, prompt: str, tz: str, state: str, result: str = "", delivery: str = "", transcript: str = "") -> None`; `async def jobs_meta_get(conn, schedule_id: str) -> dict | None`; `async def jobs_meta_list(conn) -> list[dict]`; `async def jobs_meta_update(conn, schedule_id: str, **fields) -> None`; `async def jobs_meta_running(conn) -> list[dict]` (state='running', sets state='interrupted').
- Produces: `async def transcript_search(conn, query: str, limit: int = 10) -> list[tuple[str, str]]` (session_id, message JSON) using `LIKE '%' || ? || '%'` on `transcripts.message`, newest first.

- [ ] **Step 1: Write the failing tests**

In `tests/test_db.py`: memory set/list/delete round-trip; value cap raises; 201st key raises; digest caps at `MEMORY_DIGEST_CHARS` and truncates values. `turn_insert` + `turns_report` for all/week/day periods with known timestamps. jobs_meta upsert/get/list/update/running cycle. `transcript_search` finds a known message and misses an absent one; respects limit.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_db.py -v`
Expected: FAIL — functions not defined.

- [ ] **Step 3: Implement**

Plain SQL in `assistant/db.py` following Task 2's style; `updated_at` = `datetime.now(UTC).isoformat()` on memory writes; `memory_digest` builds from `memory_all` newest-first.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_db.py -v && uv run ruff check assistant/db.py`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/db.py tests/test_db.py
git commit -m "feat(assistant): memory, turns, jobs_meta, transcript-search repositories"
```

---

### Task 4: DbSessionWriter

**Files:**
- Create: `assistant/transcripts.py`
- Test: `tests/test_transcripts.py` (new)

**Interfaces:**
- Produces: `class DbSessionWriter` — `__init__(self, db_path: Path)`; attrs `session_id: str` and `name: str` (same value, format `{YYYYmmddTHHMMSS}-{hex4}` like today's file stems); `__enter__`/`__exit__`; `def write(self, message: ConversationMessage) -> None` inserting `(session_id, seq, ts, message.serialize() JSON)` with a per-writer seq counter. Duck-types imp's writer seam (`Context.writer`) — sync `sqlite3` connection with WAL + busy_timeout pragmas; on `sqlite3.Error` disables itself with a stderr warning exactly like `imp/adapters/session.py` does.

- [ ] **Step 1: Write the failing test**

`tests/test_transcripts.py`: write a system + user + assistant message through a `Context(config, prompt, writer=DbSessionWriter(tmp_db))`; assert three rows in `transcripts` with seq 0..2, session_id == writer.name; `search_transcripts` (async, Task 3) finds the user text; a second writer gets a distinct session_id; a locked/erroring DB disables persistence without raising.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/test_transcripts.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`assistant/transcripts.py`; build the `Context` exactly as `assistant/app.py:Session.open` does today to prove seam compatibility. See imp/adapters/session.py for the disable-on-error pattern to mirror.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_transcripts.py tests/test_session.py -v && uv run ruff check assistant/transcripts.py`
Expected: PASS (imp's own session tests untouched and green), clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/transcripts.py tests/test_transcripts.py
git commit -m "feat(assistant): DB-backed session writer over the transcripts table"
```

---

### Task 5: Outbox

**Files:**
- Create: `assistant/outbox.py`
- Test: `tests/test_outbox.py` (new)

**Interfaces:**
- Produces: `class Outbox` — `__init__(self, sender: Callable[[str], Awaitable[object]])`; `def turn_scope(self) -> AbstractAsyncContextManager` (marks a turn active; on exit, deferred deliveries resume); `async def submit(self, text: str) -> None` (enqueue a job result, FIFO); internal worker task started/stopped by `async def start(self)` / `async def stop(self)`. While a turn is active, items wait; delivery failures are logged and the item is dropped (job results are also recorded in `jobs_meta.delivery` by Task 8 — the outbox never blocks a turn).

- [ ] **Step 1: Write the failing tests**

`tests/test_outbox.py`: with an event-ordered fake sender — (a) two submits with no turn active deliver in order; (b) submit during a `turn_scope` delivers only after the scope exits; (c) a sender that raises drops the item and later items still deliver; (d) `stop` drains pending items.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_outbox.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

`asyncio.Queue` + worker task + an `asyncio.Event` cleared while any turn is active (counting scope for nesting safety).

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_outbox.py -v && uv run ruff check assistant/outbox.py`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/outbox.py tests/test_outbox.py
git commit -m "feat(assistant): outbox with turn-deferred job-result delivery"
```

---

### Task 6: aiogram transport + telegramify markdown

**Files:**
- Rewrite: `assistant/adapters/telegram.py`, `assistant/adapters/markdown.py`
- Modify: `assistant/adapters/__init__.py`
- Test: rewrite `tests/test_telegram.py`, `tests/test_markdown.py`

**Interfaces:**
- Produces: `TelegramBot` (wraps `aiogram.Bot`; same constructor `__init__(self, token: str, timeout: float = 60.0, max_bytes: int = DEFAULT_MAX_HTTP_BYTES)`) with: `async def send_message(self, chat_id: int, text: str) -> int` (markdown→entities via `render`, falls back to plain text on API rejection); `async def send_text(self, chat_id: int, text: str) -> list[int]` (long/markdown answers via `render_long`: text chunks as entity messages, `File` items via `send_document`); `async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool` (entities; plain-text retry; `False` on both failures); `async def send_chat_action`, `async def send_document(self, chat_id, data: bytes, filename: str, caption: str = "") -> int`, `async def get_me(self) -> dict`, `async def get_updates(self, offset: int) -> list[dict]`, `async def download(self, file_id: str) -> bytes` (enforces `max_bytes`), `async def close(self) -> None`. `TelegramError(RuntimeError)` wraps aiogram `TelegramAPIError`.
- Produces in markdown.py: `def render(text: str) -> tuple[str, list[dict]]` (telegramify `convert()`; entities as `e.to_dict()`); `async def render_long(text: str, max_message_length: int = 4090) -> list[RenderedText | RenderedFile]` where `RenderedText(text: str, entities: list[dict])` and `RenderedFile(file_name: str, file_data: bytes, caption: str, caption_entities: list[dict])` are frozen dataclasses; `MAX_MESSAGE_CHARS = 4096` stays. `to_html`, `chunks`, `units` and the lxml converter are deleted.
- Consumes: ui.py (Task 7), uploads/main (Task 13) use only this surface.

- [ ] **Step 1: Write the failing tests**

`tests/test_markdown.py`: `render("**b** `c`")` returns text without asterisks/backticks and entities containing `bold` and `code` types; a fenced code block yields a `pre` entity with language; `render_long` on a >4096-unit markdown string yields ≥2 `RenderedText` items each within the limit; a long fenced code block yields a `RenderedFile` with the right file_name. `tests/test_telegram.py`: with an `aiogram` fake bot injected (constructor gains optional `client=None`), `send_message` passes `entities=` and no `parse_mode`; on entity rejection retries plain; `edit_message` returns `False` after both attempts fail; `download` raises `TelegramError` when bytes exceed `max_bytes`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_telegram.py tests/test_markdown.py -v`
Expected: FAIL — imports/symbols missing.

- [ ] **Step 3: Implement**

telegramify global runtime config at import: strip heading emoji (`markdown_symbol.heading_level_1..4 = ""`), unordered list marker `"-"`. aiogram `DefaultBotProperties` not used — we never set parse_mode. All aiogram calls wrapped so any `TelegramAPIError` becomes `TelegramError(f"{method} failed: {exc}")` (no token leakage).

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_telegram.py tests/test_markdown.py -v && uv run ruff check assistant/adapters/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/adapters/telegram.py assistant/adapters/markdown.py assistant/adapters/__init__.py tests/test_telegram.py tests/test_markdown.py
git commit -m "feat(assistant): aiogram transport with telegramify entity rendering"
```

---

### Task 7: turn UX — status message, tool log, cost summary

**Files:**
- Rewrite: `assistant/adapters/ui.py`
- Test: rewrite `tests/test_ui.py`

**Interfaces:**
- Produces: `TelegramUIAdapter` keeps its surface (`begin/handle/flush/end_turn/answer`, `StatusBuffer(max_chars)`) but renders: `begin()` sends `🧠 thinking…`; TOOL_START appends `f"{tool_name}  {subject}"` via `tool_subject(tool_name, tool_args) -> str`; THINKING/REASONING/MODEL_RESPONSE append nothing; ERROR appends `*error:* {message}`; buffer keeps the header + last `STATUS_LINES` tool lines + optional `… +{n} earlier` overflow line.
- Produces: `def tool_subject(name: str, args: dict | None) -> str` — pinned map: `read_file`/`write_file`/`str_replace`/`list_dir` → `path` (basename-if-longer-than-40); `run_shell` → first line of `command` ≤ 40; `web_fetch` → URL host; `web_search` → `query` ≤ 40; `send_file` → `path` basename; `schedule_job`/`unschedule_job` → `id`; `ask` → question first line ≤ 40; `memory_*`/`search_transcripts`/`cost_report`/`queue_status` → `key`/`query`/`period` respectively; unknown → `""`.
- Produces: `def turn_summary(tools: int, seconds: int, ok: bool, cost_usd: float | None) -> str` — exact formats from Global Constraints.

- [ ] **Step 1: Write the failing tests**

`tests/test_ui.py` golden-style with a fake transport: `begin` → `🧠 thinking…`; first TOOL_START flips header to `🧠 working` and appends the mapped line; 12 tool calls keep the last 8 with `… +4 earlier`; a REASONING event changes nothing; end_turn collapses to `turn_summary(3, 47, True, 0.0134)` == `"✓ done · 3 tools · 47 s · $0.0134"`; `turn_summary(3, 12, False, None)` == `"✗ failed · 3 tools · 12 s"`; subject map cases (long path truncation, run_shell multiline command takes first line).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_ui.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Keep the throttle/flush machinery (edit interval, dirty flag, forced flush, one-recreate-on-persistent-edit-failure) unchanged in behavior — it already matches the new render pipeline since `edit_message` accepts markdown-ish text (Task 6 renders entities internally). `turn_summary` moves here from `main.py` (delete it there in Task 13).

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_ui.py -v && uv run ruff check assistant/adapters/ui.py`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/adapters/ui.py tests/test_ui.py
git commit -m "feat(assistant): thinking status, tool-only live log, cost-aware turn summary"
```

---

### Task 8: scheduler core on APScheduler

**Files:**
- Rewrite: `assistant/scheduler.py` (delete jobstore.py usage there; `assistant/jobstore.py` itself is deleted in Task 14)
- Test: rewrite `tests/test_scheduler.py`

**Interfaces:**
- Produces: `def build_scheduler(home: Path, tz: str) -> AsyncIOScheduler` — `SQLAlchemyJobStore` on the same `state.db` (SQLAlchemy `URL.create("sqlite", database=str(home / STATE_DB_NAME))`, `connect_args={"timeout": 30}`), `AsyncIOScheduler(timezone=ZoneInfo(tz))`, `job_defaults=dict(misfire_grace_time=60, coalesce=True, max_instances=1)`.
- Produces: `@dataclass(slots=True) class JobContext` — `app: AssistantApp`, `outbox: Outbox`, `semaphore: asyncio.Semaphore`, `db: aiosqlite.Connection`; module-level `def set_context(ctx: JobContext) -> None` / `_CONTEXT: JobContext | None`.
- Produces: `async def run_scheduled_job(schedule_id: str, prompt: str) -> None` (module-level; APSerializer resolves it by import): claims semaphore; `jobs_meta_update(state="running")`; fresh `Session` via `DbSessionWriter` + agent without `ask` (same construction as `assistant/scheduler.py:118-147` today); sums `event.usage`; records a `turns` row (kind="job"); on success `jobs_meta_update(state="scheduled", result=answer, transcript=session_id)` for recurring / `state="done"` for one-shots; failures → `state="error"`, `result=f"Job {schedule_id} failed: {exc}"`; delivery via `outbox.submit(f"⏰ {schedule_id}\n\n{result}")` then `delivery="sent"` (or `"failed"` logged).
- Produces: `async def startup_recovery(ctx: JobContext) -> None` — `jobs_meta_running()` rows reported to the owner as interrupted (direct send, not outbox), state left `interrupted`.
- Consumes: `AssistantApp` (Task 12 shape: `config`, `agent`, `build_prompt`, `chat_id`, `bot`), `DbSessionWriter` (Task 4), repos (Tasks 2–3), Outbox (Task 5).

- [ ] **Step 1: Write the failing tests**

`tests/test_scheduler.py` with a real `AsyncIOScheduler` on tmp SQLite (no fake scheduler): `build_scheduler` starts and a job added with `add_job(run_scheduled_job, DateTrigger(run_date=now+1s), args=["j1", "prompt…"])` fires within ~2 s when `_CONTEXT` holds a minimal fake app (agent yielding one MODEL_RESPONSE with `Usage(10, 5, 15, 0.01)` and a fake outbox capturing submits) — assert jobs_meta row `state="done"`, turns row inserted, outbox got `⏰ j1` text. Second test: context agent raising → `state="error"` and outbox still notified. Third: `startup_recovery` with a pre-seeded `running` row sends the interrupted notice.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_scheduler.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Note: `run_scheduled_job` must tolerate `_CONTEXT is None` (stale DB row, e.g. after a refactor) by logging and marking `error`. The non-interactive system-prompt suffix from today's `_execute` is kept verbatim.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_scheduler.py -v && uv run ruff check assistant/scheduler.py`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/scheduler.py tests/test_scheduler.py
git commit -m "feat(assistant): APScheduler-driven concurrent job execution with outbox delivery"
```

---

### Task 9: schedule tools on APScheduler (+ cron)

**Files:**
- Rewrite: `assistant/tools/schedule.py`
- Modify: `assistant/tools/__init__.py`
- Test: rewrite `tests/test_schedule.py`

**Interfaces:**
- Produces: `ScheduleJob(Tool)` — params `prompt`, `at`, `at_local`, `every`, **`cron`** (new: 5-field crontab in the owner's TZ), `id`; exactly one of at/at_local/every/cron required; constructor takes `tz: str`, `scheduler: AsyncIOScheduler` (or a fake exposing `add_job`/`get_jobs`/`remove_job`), and `db: aiosqlite.Connection`. Persists via `scheduler.add_job(run_scheduled_job, trigger, args=[job_id, prompt], id=job_id, replace_existing=True)` + `jobs_meta_upsert(...)` (Task 3 repo on the shared connection).
- Produces: `UnscheduleJob(Tool)` — `scheduler.remove_job(id)` + `jobs_meta_update(state="cancelled")`; JobNotFoundError → friendly ToolResult error listing `scheduler.get_jobs()` ids.
- Produces: `build_assistant_tools(config, fs, prompt_user, http, sender, tz, scheduler, db)` — two new kwargs (`scheduler: AsyncIOScheduler`, `db: aiosqlite.Connection`); registers the two tools.
- Cron validation: `CronTrigger.from_crontab(expr, timezone=ZoneInfo(tz))` raises `ValueError` → ToolResult error.

- [ ] **Step 1: Write the failing tests**

`tests/test_schedule.py` with a fake scheduler recording `add_job` calls and tmp `state.db`: at / at_local / every / cron each schedule with the right trigger type (cron `"0 9 * * *"` → CronTrigger with hour=9); two-of-them and none-of-them → error ToolResult; auto-id uses the `job-YYYYmmdd-HHMMSS` format and never collides (replaces with explicit id, suffixes generated ids); `unschedule_job` on an existing id cancels and marks `cancelled`; on a missing id returns error containing current ids.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_schedule.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Keep `resolve_local_time`, `generate_id`, `unique_id`, `valid_id` — move them from `jobstore.py` into `assistant/tools/schedule.py` (or a small `assistant/jobids.py`); `jobstore.py` dies in Task 14.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_schedule.py -v && uv run ruff check assistant/tools/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/tools/schedule.py assistant/tools/__init__.py tests/test_schedule.py
git commit -m "feat(assistant): schedule/unschedule tools over APScheduler with cron support"
```

---

### Task 10: memory tools + prompt digest

**Files:**
- Create: `assistant/tools/memory.py`
- Modify: `assistant/prompt.py`, `assistant/tools/__init__.py`
- Test: `tests/test_memory_tools.py` (new), `tests/test_prompt.py`

**Interfaces:**
- Produces: `MemorySet` (`key`, `value`), `MemoryList` (no params), `MemoryDelete` (`key`) — async, backed by Task 3 repos over a shared `db: aiosqlite.Connection` constructor kwarg; cap violations return ToolResult errors quoting the limits.
- Produces: `assistant/prompt.py` — `def memory_section(digest: str) -> str` returning `"\n\n## Memory\n\n" + digest` when digest is nonempty else `""`; `build_prompt`-side integration happens in Task 12's `app.build_prompt`.

- [ ] **Step 1: Write the failing tests**

`tests/test_memory_tools.py`: set → list round-trip; set with a 3000-char value → error mentioning 2048; delete missing key → ok=False; digest reflects writes newest-first with truncation. `tests/test_prompt.py`: `memory_section("")` == `""`; nonempty digest is wrapped as specified.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_memory_tools.py tests/test_prompt.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

Register the three tools in `build_assistant_tools` (no new kwargs — `db` already arrives in Task 9).

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_memory_tools.py tests/test_prompt.py -v && uv run ruff check assistant/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/tools/memory.py assistant/tools/__init__.py assistant/prompt.py tests/test_memory_tools.py tests/test_prompt.py
git commit -m "feat(assistant): durable agent memory tools with system-prompt digest"
```

---

### Task 11: inspect tools

**Files:**
- Create: `assistant/tools/inspect.py`
- Modify: `assistant/tools/__init__.py`
- Test: `tests/test_inspect_tools.py` (new)

**Interfaces:**
- Produces: `ListJobs` (no params) — joins `scheduler.get_jobs()` (id, next_run_time) with `jobs_meta_list()` (label, state, last result truncated to 200 chars) → one line per job; `SearchTranscripts` (`query`, `limit` default 10) via `transcript_search`; `CostReport` (`period` ∈ day/week/month/all, default all) via `turns_report`, content like `turns 12 · in 45k · out 8k · $0.42` (k-abbreviated, cost 4 decimals); `QueueStatus` (no params) — `queue_count_waiting` + "active turn" from an `is_turn_active: Callable[[], bool]` constructor kwarg.
- Constructors take `db: aiosqlite.Connection`; `ListJobs` also `scheduler`; `QueueStatus` also the callable. All registered in `build_assistant_tools` (no new kwargs beyond Task 9's).

- [ ] **Step 1: Write the failing tests**

`tests/test_inspect_tools.py` against tmp state.db + fake scheduler: seeded jobs_meta + schedule rows → ListJobs lists next fire and state; SearchTranscripts finds/misses; CostReport sums seeded turns for each period; QueueStatus reports 2 waiting + turn active.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_inspect_tools.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_inspect_tools.py -v && uv run ruff check assistant/tools/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/tools/inspect.py assistant/tools/__init__.py tests/test_inspect_tools.py
git commit -m "feat(assistant): list_jobs, search_transcripts, cost_report, queue_status tools"
```

---

### Task 12: composition root, bootstrap-on-kv, Session→DbSessionWriter

**Files:**
- Modify: `assistant/app.py`, `assistant/bootstrap.py`
- Test: modify `tests/test_assistant_app.py`, `tests/test_bootstrap.py`

**Interfaces:**
- Produces: `build_assistant(assistant_config, chat_id)` (same asynccontextmanager shape) now also opens `open_db(home / STATE_DB_NAME)`, builds `Outbox` (sender = `bot.send_text`), `build_scheduler`, `asyncio.Semaphore(assistant_config.max_concurrent_jobs)`, `JobContext` + `set_context`, and yields an `AssistantApp` whose `Session.open` uses `DbSessionWriter(home / STATE_DB_NAME)`; `AssistantApp.execution_lock` deleted; new fields `db`, `outbox`, `scheduler`. `app.build_prompt` appends `memory_section(await memory_digest(db))`.
- Produces: `bootstrap.read_state(home) -> dict` / `write_state(home, updates) -> dict` — same signatures, now backed by the kv table through short-lived sync sqlite3 connections (WAL + timeout pragmas); `fingerprint`/`tailored` become kv keys.
- Consumes: Tasks 2–11.

- [ ] **Step 1: Write the failing tests**

Adapt existing tests: `test_assistant_app.py` asserts the yielded app exposes `db`/`outbox`/`scheduler`, no `execution_lock` attribute, `/new`-equivalent `reset()` returns the writer's `name`, and `build_prompt` contains `## Memory` only when memory rows exist. `test_bootstrap.py` keeps passing with kv-backed state (round-trip fingerprint/tailored through `read_state`/`write_state` on tmp home with initialized schema).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_assistant_app.py tests/test_bootstrap.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`ensure_home` keeps creating content dirs but no longer `jobs/`; startup ordering (context registration before scheduler start) is `main.py`'s job (Task 13) — here only construction.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/test_assistant_app.py tests/test_bootstrap.py -v && uv run ruff check assistant/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/app.py assistant/bootstrap.py tests/test_assistant_app.py tests/test_bootstrap.py
git commit -m "feat(assistant): v2 composition root — db, outbox, scheduler, memory-prompt"
```

---

### Task 13: polling, intake, uploads, whoami, TurnRunner

**Files:**
- Rewrite: `assistant/main.py`, `assistant/uploads.py`
- Create: `assistant/intake.py`
- Test: rewrite `tests/test_uploads.py`, `tests/test_whoami.py`; create `tests/test_intake.py`; adapt `tests/test_telegram.py` handler-level fakes

**Interfaces:**
- Produces: `assistant/intake.py` — `def normalize_attachment(message: dict) -> dict | None` (keys: `kind` ∈ document/photo/video/audio/video_note/animation/sticker/voice, `file_id`, `file_name`, `forward_origin: str | None`); `def route_message(message: dict, *, owner_id: int, question_pending: bool, busy: bool) -> Routed` where `Routed` is a frozen dataclass (`command: str | None`, `answer: str | None`, `text: str | None`, `attachment: dict | None`, `ignore: bool`); `class AlbumBuffer` — `def __init__(self, wait_s: float = 1.5)`, `async def add(self, message: dict) -> None`, `async def flush_due(self) -> list[list[dict]]` (groups by `media_group_id`, returns completed groups); `class Intake` — `__init__(self, db, bot, chat_id)`, `async def accept(self, message: dict) -> str | None` (dedup via kv `last_message_id` monotonic guard, voice transcription path unchanged, pushes queue rows, returns the ack text or None when it runs immediately).
- Produces: `main.py` — aiogram `Dispatcher` with a single private-chat owner message handler: dedup → question routing (AskRouter, unchanged semantics) → command (`/new`, `/status` showing `session.name` + real token usage from last turn) → `Intake.accept` → album buffering; a turn-worker task draining `queue_claim_next` through a rewritten `TurnRunner` (uses `ui.turn_summary`, accumulates `event.usage` into a `turns` row, runs inside `outbox.turn_scope()`); startup: interrupted-queue report + `startup_recovery` + scheduler.start + outbox.start; `whoami()` over aiogram `Bot.get_updates` printing sender ids (behavior unchanged).
- Produces: `uploads.py` — `async def handle(self, attachments: list[dict], caption: str) -> str | None` saving every kind via `bot.download(file_id)` with kind-prefixed names (voice still STT + delete-on-success); forward_origin folded into the synthesized prompt ("Owner forwarded …").

- [ ] **Step 1: Write the failing tests**

`tests/test_intake.py`: `normalize_attachment` maps a video message, an audio m4a (MIME audio/mp4) message, and a document message; sticker/animation map; plain text → None. `route_message` owner/group/other-sender, command, question-pending, attachment cases. `test_redelivered_update_not_duplicated`: same message_id twice → second returns ignore. `test_album_merge_interleaved`: three same-`media_group_id` messages with a 0.2 s straggler plus an interleaved text message → one group of three, text unaffected. `tests/test_uploads.py::test_m4a_audio_and_document`: both forms save to inbox/ and produce a prompt; voice path keeps STT semantics. TurnRunner-level: fake agent emitting MODEL_RESPONSE with Usage → `turns` row written with cost and the summary used the cost segment.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/test_intake.py tests/test_uploads.py tests/test_whoami.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

aiogram message objects enter as `message.model_dump(exclude_none=True)` at the handler boundary; everything downstream is dict-shaped (testable without aiogram). `validate_queue`, `valid_request`, `queued_notice`'s state-dict logic are replaced by their db/intake equivalents — keep `queued_notice` verbatim.

- [ ] **Step 4: Run the whole suite**

Run: `uv run python -m pytest && uv run ruff check .`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add assistant/main.py assistant/intake.py assistant/uploads.py tests/
git commit -m "feat(assistant): aiogram polling, dedup+album intake, all-kind uploads, v2 turn runner"
```

---

### Task 14: strip v1 remains, deployment, docs

**Files:**
- Delete: `assistant/jobstore.py`, `assistant/deploy/S99assistant`
- Modify: `assistant/README.md`, `assistant/.env.example`, `AGENTS.md` (assistant paragraph), `assistant/deploy/assistant.service` (unchanged unless it references removed paths), `Dockerfile` (assistant target unchanged unless it copies S99)
- Test: full suite (docs task — verification is suite + grep)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Verify nothing references deleted modules**

Run: `grep -rn "jobstore\|S99assistant\|state.json" --include="*.py" assistant/ tests/ imp/`
Expected: no matches in code (README mentions only historical).

- [ ] **Step 2: Update docs**

README: architecture (state.db schema table, APScheduler, aiogram, outbox), tools list (memory/inspect/cron), deploy section (armbian aarch64 first-class, busybox/musl/SCP sections removed, backup = copy state.db while stopped), refreshed smoke checklist (concurrent job+turn, redelivery dedup, album, m4a, cost line). `.env.example`: add `IMP_MAX_CONCURRENT_JOBS=2`. Root `AGENTS.md`: rewrite the assistant bullet to v2 (keep it lean — this file feeds imp's own system prompt).

- [ ] **Step 3: Run everything**

Run: `uv run python -m pytest && uv run ruff check . && uv run python -m assistant --help`
Expected: PASS, clean, usage prints.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "chore(assistant): remove v1 job store and busybox target; v2 docs"
```

---

## Task dependency order

1 → 2 → 3 → 4/5 (parallel) → 6 → 7 → 8 → 9 → 10/11 (parallel) → 12 → 13 → 14.
Tasks 8–11 need the `AssistantApp` shape only as a lightweight protocol in tests (fakes); the real rewiring happens in 12.
