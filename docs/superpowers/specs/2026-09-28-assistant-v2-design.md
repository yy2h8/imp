# assistant v2 — design

Date: 2026-09-28
Status: approved in brainstorming; awaiting spec review

## Problem

The Telegram assistant (assistant/, built on imp as a library) has seven gaps:

1. Turn status shows reasoning text; the owner wants a single "thinking" status
   message that stays until the turn ends.
2. The live log mixes reasoning, model responses, and verbose tool arguments;
   it should contain only tool uses, one short line each.
3. No cost tracking per turn. imp's `AgentEvent.token_usage` is a char-based
   estimate; the provider (OpenRouter) reports real token counts and USD cost.
4. Markdown rendering and file transfer are both half-working. Root causes are
   split: the hand-rolled httpx2 transport (multipart uploads/downloads) and
   the custom markdown→HTML converter.
5. A scheduled job blocks interactive turns: one `execution_lock` serializes
   everything (assistant/app.py). Jobs must run concurrently with turns and
   each other; results must wait for the active turn before delivery. More
   schedule paradigms are wanted (one-shot, recurring, cron).
6. State is scattered across `state.json` and `jobs/*.json`. Move file-based
   state into SQLite; give the agent tools over it.
7. Deployment targets a BusyBox/musl Pi Zero; the real target is now plain
   glibc Linux (armbian aarch64 first).

## Decisions (with rationale)

| Decision | Choice | Why |
|---|---|---|
| Telegram framework | aiogram 3.31+ | Async-first, routers/middleware, full API types, own polling lifecycle. (python-telegram-bot considered: httpx-based but heavier; keeping the custom transport rejected — both markdown and file transfer were observed failing.) |
| Markdown | telegramify-markdown 1.4+ | Purpose-built for LLM output → Telegram; entity-based output (text, entities) with no parse_mode; `telegramify()` splits long text entity-safely. The custom lxml converter is deleted. |
| Scheduler | APScheduler 3.11 stable | Durable (SQLAlchemyJobStore), stable, triggers for all three paradigms, misfire handling. APScheduler 4 was evaluated and rejected: still alpha (4.0.0a6). A custom ~150-line dispatcher was considered; user chose the library. |
| DB | single `state.db` (SQLite, WAL) in IMP_HOME | One backup artifact. Our tables via aiosqlite; APScheduler's `apscheduler_jobs` table lives in the same file via SQLAlchemyJobStore (names don't collide). |
| Migration | fresh start | v1 `state.json` + `jobs/*.json` ignored, left on disk. Personal bot; owner re-creates recurring jobs. |
| Memory | injected + tools | kv-backed memory with tools and a size-capped digest in the system prompt (rebuilt each turn already). |
| Transcripts | into `state.db` | Replaces `sessions/*.jsonl`. Assistant-side `DbSessionWriter` duck-types imp's writer seam (sync sqlite3, WAL); imp core untouched. |
| Approach | phased in-place rewrite on trunk | Six phases (below) rather than a greenfield package; existing suite adapted per phase. |

## Component map

```
assistant/
├── main.py          # aiogram Dispatcher polling + wiring (replaces PollLoop)
├── app.py           # composition root; execution_lock removed (Outbox + job semaphore instead)
├── config.py        # + IMP_MAX_CONCURRENT_JOBS (default 2); memory caps as constants
├── bootstrap.py     # unchanged, except fingerprint → kv table
├── db.py            # NEW: aiosqlite open/migrate + repos (kv, queue, turns, transcripts, memory, jobs_meta)
├── outbox.py        # NEW: ordered delivery; job results defer while a turn is active
├── scheduler.py     # APScheduler wiring + module-level async job task fn
├── transcripts.py   # NEW: DbSessionWriter over the transcripts table (imp writer seam, duck-typed)
├── uploads.py       # downloads via aiogram; accepts every attachment kind; albums merged into one entry
├── prompt.py        # + size-capped memory digest appended to BASE_PROMPT
├── adapters/
│   ├── telegram.py  # thin aiogram Bot wrapper keeping today's method surface (send/edit/action/document/file)
│   ├── markdown.py  # telegramify-markdown: convert() → (text, entities); telegramify() for long answers
│   ├── ui.py        # new status rendering (thinking message + tool-only log)
│   └── stt.py       # unchanged
├── tools/           # schedule.py rewritten on APScheduler; + memory.py, inspect.py; send_file.py aiogram-backed
└── deploy/          # assistant.service + compose.yaml stay; S99assistant deleted
```

## Data model — state.db (WAL)

Our tables (aiosqlite):

- `kv(key TEXT PRIMARY KEY, value TEXT)` — fingerprint, tailoring flag, `last_message_id` (redelivery dedup; the update offset itself belongs to aiogram's polling, not persisted state).
- `memory(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)` — agent memory. Caps: 200 keys, 2 KB per value; prompt digest ≤ 1.5 KB (oldest entries truncated out of the digest, never silently deleted).
- `queue(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, payload TEXT, state TEXT, created_at TEXT)` — request FIFO. States: waiting / active / done. Done rows are pruned.
- `turns(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, session_id TEXT, model TEXT, in_tokens INTEGER, out_tokens INTEGER, cost_usd REAL, tools INTEGER, seconds REAL, ok INTEGER)` — one row per turn; kinds: interactive / job / bootstrap.
- `transcripts(session_id TEXT, seq INTEGER, ts TEXT, message TEXT, PRIMARY KEY (session_id, seq))` — one row per conversation message (JSON, same shape as today's jsonl lines). `session_id` keeps today's stamp-token format so `/status` and `jobs_meta.transcript` stay readable.
- `jobs_meta(schedule_id TEXT PRIMARY KEY, label TEXT, prompt TEXT, tz TEXT, result TEXT, delivery TEXT, transcript TEXT, updated_at TEXT)` — our job presentation/state; next-fire time comes from APScheduler.

APScheduler's `apscheduler_jobs` table (SQLAlchemyJobStore, same file, its own
sync connection). Job args are plain strings (schedule_id, prompt) — pickled by
the store, which makes `state.db` trusted-local-only (already the bot's model).

Schema migrations: `PRAGMA user_version` + ordered DDL steps in db.py.

Indexes:

- `queue(state, id)` — FIFO drain + restart scan.
- `turns(ts)`, `turns(kind, ts)` — `cost_report` aggregations.
- `kv` and `memory` are covered by their PRIMARY KEYs; `transcripts` by its
  composite PK. `search_transcripts` uses `LIKE '%q%'`, which no index
  accelerates — a deliberate table scan (personal-bot volume: milliseconds).

## Uploads and attachments

Accept anything the owner sends. Kinds: `document`, `photo`, `video`, `audio`
(incl. m4a — arrives as `audio` with MIME audio/mp4 when sent playable, or as
`document` when sent as file; both must work), `video_note`, `animation`,
`sticker`. Saved to `inbox/` with kind-prefixed names; captions still become
the turn prompt. Forwarded messages record `forward_origin` in the synthesized
prompt ("Owner forwarded a photo from …"). Albums: messages sharing a
`media_group_id` are merged into a single queue entry — all files saved, one
turn ("Owner sent 3 photos…"). The handler buffers briefly (Telegram delivers
album items as separate updates seconds apart) before enqueueing the merged
entry. Anything genuinely unhandleable gets an explicit reply, never silence.

## Agent tools

New:

- `memory_set(key, value)` / `memory_list()` / `memory_delete(key)` — durable notes; digest injected into the system prompt each turn.
- `list_jobs()` — id, label, paradigm, next fire, state, last result.
- `search_transcripts(query, limit)` — plain `LIKE` over transcripts.message; FTS5 is a non-goal.
- `cost_report(period: day|week|month|all)` — aggregates from `turns`.
- `queue_status()` — queue depth and pending items.

Rewritten:

- `schedule_job` / `unschedule_job` on APScheduler: `at` one-shot (DateTrigger), `every` interval seconds (IntervalTrigger), `cron` expression (CronTrigger, owner TZ via IMP_TZ). Persists jobs_meta alongside the schedule.
- `send_file` — aiogram multipart under the hood.

## Concurrency and delivery

- Interactive turns: one at a time. The aiogram message handler enqueues accepted
  prompts/attachments into `queue` (with today's "Принято — в очереди" acks);
  a single TurnRunner drains the FIFO. AskRouter question routing unchanged.
- Jobs: APScheduler (AsyncIOScheduler) fires each due job as its own asyncio
  task — fresh session written to `transcripts`, `ask` tool removed, same
  isolation as today. Bounded by a semaphore, `IMP_MAX_CONCURRENT_JOBS`
  (default 2).
- The whole-bot `execution_lock` is removed. Turns and jobs interleave freely;
  safety comes from fresh per-job sessions, stateless tools, and SQLite WAL.
- Outbox: one ordered delivery lane for job results. While any interactive
  turn is active, job results wait; delivered FIFO between turns. Turn output
  itself goes direct.
- Redelivery dedup: aiogram does not persist update offsets; after a crash
  Telegram re-sends unconfirmed updates. A monotonic `last_message_id` guard
  in `kv` prevents duplicate queue entries.
- Interruptions on restart: `queue` rows stuck in `active` → reported as
  interrupted, never replayed. `jobs_meta` rows stuck `running` → reported as
  interrupted one-shots; recurring schedules fire again at the next interval.
- APScheduler policy: `misfire_grace_time=60s`, `coalesce=True`,
  `max_instances=1` per schedule.
- Job task fn is a module-level coroutine (APScheduler resolves it by import);
  the composition root registers itself in a module-level context holder
  (single-process bot).

## Turn UX

- Turn start: status message `🧠 thinking…` created eagerly; stays until turn
  end; typing indicator refreshed alongside (as today).
- Live log: tool uses only, appended to a monospace block, one line per tool
  with a per-tool subject extractor (path / first line of command / hostname /
  query / filename / job id — no string-argument blobs). Last 8 lines kept;
  `… +N earlier` overflow marker. Header switches `🧠 thinking…` → `🧠 working`.

```
🧠 working
read     imp/agent/model.py
shell    ruff check .
write    assistant/db.py
… +4 earlier
```

- REASONING and MODEL_RESPONSE text never appear in the status.
- Turn end: collapse to `✓ done · 3 tools · 47 s · $0.0134` — cost segment
  omitted when the provider reports none. Failures: `✗ failed · 3 tools · 12 s`
  plus the error message, as today.
- Final answers: `telegramify()` — entity-safe splitting for long output.
  When it yields `File` items (extracted code blocks), they are sent via
  `send_document` with their captions.
- `whoami` entry point stays: a manual `get_updates` print loop over the
  aiogram Bot (still "run while the bot is stopped").

## imp core changes (backward compatible)

- `imp/events.py`: new frozen dataclass `Usage(input_tokens, output_tokens,
  total_tokens, cost_usd: float | None)` and `AgentEvent.usage: Usage | None =
  None`. Usage lives in events.py so the dependency-free contract stays intact.
- `imp/agent/model.py`: parse `response.usage` (tokens; OpenRouter's `cost`
  field when present) into `ModelReply.usage`.
- `imp/agent/agent.py`: attach usage to MODEL_RESPONSE events. The assistant
  sums cost per turn → `turns` row + summary line.
- Context/SessionWriter untouched. CLI rendering ignores the new field.

## Dependencies

Added to root pyproject (single package; accepted deviation from imp's
tiny-deps principle):

- `aiogram>=3.31` (brings aiohttp, pydantic)
- `apscheduler[sqlalchemy]~=3.11` (brings SQLAlchemy)
- `aiosqlite`
- `telegramify-markdown>=1.4` (brings pyromark)

## Deployment

- Delete `assistant/deploy/S99assistant`; drop BusyBox/musl/Dropbear-SCP
  sections from README. systemd unit and Docker target stay.
- Target: generic glibc Linux; first deployment armbian aarch64. Native deps
  (lxml, pydantic-core, aiohttp) ship manylinux aarch64 wheels — no musl
  caveats.
- Backup story: stop the bot, copy `state.db`.

## Testing

- imp suite: usage parsing from mocked responses (with and without cost);
  event field defaults.
- assistant suite: db repos on tmp SQLite; Outbox ordering/defer; scheduler
  wiring with a real AsyncIOScheduler + tmp SQLite jobstore; telegram adapter
  through aiogram fakes; UI renderer golden tests; memory caps; dedup guard;
  attachment matrix (document/photo/video/audio m4a/voice/sticker, forwarded
  messages, album merge).
- Existing test_scheduler/test_telegram/test_schedule adapted or replaced.
- README smoke checklist updated for v2 (concurrent job + turn, redelivery
  dedup, outbox defer, cost line).

## Non-goals

- FTS5 / full-text search tuning.
- Multi-owner support, exactly-once external actions, persistent conversation
  resume (unchanged limits from v1).
- Migrating v1 job/state files.
- imp CLI UX changes beyond the new event field.

## Accepted risks

- APScheduler 3.x internals are sync-first; jobstore writes happen off the
  async path. For this bot's job volume (dozens) that is irrelevant.
- Pickled job args in state.db — trusted-local-only artifact.
- aiogram owns the polling loop: our durable FIFO + dedup guard replace the
  old persisted-offset semantics; a crash window can still produce one
  duplicate *acknowledgement*, never a duplicate queue entry.
