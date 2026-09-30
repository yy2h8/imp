# Status Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/status` and the startup notification show one short summary (time in `IMP_TZ`, 3 nearest jobs, context estimate, host state, OpenRouter balance), resilient to per-source failures.

**Architecture:** new module `assistant/status.py` with a single collector `collect_status(app) -> str` that never raises; each metric is gathered independently, a failure → «недоступен». `AssistantApp` gains an `http` field (the `HttpClient` already built in `build_assistant` but never stored). `/status` moves to an async path in `TurnRunner.run()`; `"pong"` in `run_bot()` is replaced with the same summary.

**Tech Stack:** pure Python 3.12 stdlib (`os.getloadavg`, `/proc/meminfo`, `shutil.disk_usage`, `zoneinfo`), existing `HttpClient` (httpx2) for OpenRouter, APScheduler introspection, aiosqlite.

**Spec:** the user's spec (2026-09-30, summarized below):

- `/status` и уведомление при запуске используют единый сборщик данных и показывают одинаковую сводку.
- Содержимое: дата/время в `IMP_TZ` (по умолчанию `Asia/Almaty`); три ближайших активных задания (описание + время следующего запуска); использованные и максимальные токены текущего контекста (оценка); CPU/RAM/диск хоста; баланс кошелька OpenRouter в USD.
- Сбой получения одного показателя не скрывает остальные: недоступное значение помечается как «недоступен».
- Ошибка отправки стартового статуса не должна мешать запуску бота.
- Секреты и технические подробности ошибок в Telegram не выводятся.
- Приёмка: `/status` показывает перечисленные показатели и не более трёх ближайших активных заданий; при запуске вместо `pong` — та же сводка; при сбое отдельного источника остальные отображаются; оценка использования контекста явно отличима от точного значения.

**User rulings (from planning Q&A):** balance source — try `GET /api/v1/credits` first (true wallet; needs a management key), fall back to `GET /api/v1/key` `limit_remaining`, else «недоступен»; summary language — Russian.

## Global Constraints

- Python 3.12 / uv; no new runtime dependencies; `uv run ruff check .` and `uv run python -m pytest` stay clean.
- Summary in Russian; unavailability marker — «недоступен»; at most 3 jobs listed.
- Context usage explicitly marked as an estimate: `~` and «(оценка)».
- No secrets or raw error text in Telegram output (details go to `_LOG.warning`).
- `collect_status` never raises — neither for `/status` nor at startup.

## Review Focus

1. `/credits` returns 403 with a regular inference key → falls back to `/key`, no crash (Task 2).
2. Hanging balance HTTP request → `wait_for` timeout, «недоступен» (Task 2).
3. «Нет заданий» ≠ «сбой планировщика» — different markers (Task 1).
4. Paused scheduler (`next_run_time=None`) — job skipped, sorting unbroken (Task 1).
5. Any failure inside `collect_status` interrupts neither `/status` nor bot startup; the API key never appears in the text (Tasks 2–3).

## Summary Template

```
*Статус*

🕐 30.09.2026 14:32 (Asia/Almaty)

⏰ Задания:
• morning-report — 30.09 18:00
• backup — 01.10 03:00

🧠 Контекст: ~12 345 / 128 000 токенов (оценка)

🖥 Хост: load 0.42 · RAM 3.9/6.3 ГБ (62%) · диск: свободно 24 ГБ (71%)

💳 OpenRouter: $74.75
```

No jobs → «нет активных заданий»; a failed source → «недоступен» in place of the value.

---

### Task 1: `assistant/status.py` — collector without network (time, jobs, context, host)

**Files:**
- Create: `assistant/status.py`
- Test: `tests/test_status.py`

**Interfaces:**
- Produces: `UNAVAILABLE: str = "недоступен"`, `NO_JOBS: str = "нет активных заданий"`;
  `async def collect_status(app: AssistantApp, now: datetime | None = None) -> str`;
  `def host_summary(home: Path) -> str` (sync, called via `asyncio.to_thread`);
  `def format_tokens(used: int, maximum: int) -> str`;
  `def _jobs_lines(scheduler, db, tz: ZoneInfo) -> list[str]`.

- [ ] **Step 1: write failing tests** — `now=None` → line with `%d.%m.%Y %H:%M` and the tz name from `app.assistant.tz`; fixed `now` → deterministic string. `_jobs_lines`: fake scheduler (`SimpleNamespace(id, next_run_time)` with aware datetimes) + `jobs_meta` rows in a real tmp db (`open_db` + `jobs_meta_upsert`) → top-3 by time, label from `label`; 5 jobs → exactly 3 lines; job without a meta row → `id` shown; `next_run_time=None` → skipped; empty → `NO_JOBS`; `scheduler=None` → `UNAVAILABLE`; time converted to app tz. `format_tokens(12345, 128000)` → contains `~12 345 / 128 000` and `(оценка)`. `host_summary(tmp_path)` → contains `load`, `RAM`, `диск`. Integration: `collect_status` on the fixture app (no scheduler/http) → every section present, failed ones = «недоступен», nothing raised.
- [ ] **Step 2: run** `uv run python -m pytest tests/test_status.py -v` → FAIL (no module `assistant.status`).
- [ ] **Step 3: implement** — `_jobs_lines` takes `scheduler.get_jobs()`, filters `next_run_time`, `astimezone(tz)`, `label` from `jobs_meta_list` (map by `schedule_id`), label truncated to 40 chars + `…`; RAM — parse `/proc/meminfo` (`MemTotal`, `MemAvailable`), disk — `shutil.disk_usage(home)`; each section in its own `try/except Exception` with `_LOG.warning`.
- [ ] **Step 4: run** `uv run python -m pytest tests/test_status.py -v` → PASS.
- [ ] **Step 5: commit** `feat(assistant): status collector core`

### Task 2: OpenRouter balance + `http` wiring into `AssistantApp`

**Files:**
- Modify: `assistant/app.py:105-125` (field), `assistant/app.py:263-277` (pass it)
- Modify: `assistant/status.py`
- Test: `tests/test_status.py`, `tests/test_assistant_app.py`

**Interfaces:**
- Consumes: `collect_status` from Task 1.
- Produces: `async def openrouter_balance(http: HttpClient, api_key: str, timeout: float = BALANCE_TIMEOUT_S) -> float | None`; `BALANCE_TIMEOUT_S: float = 10.0`; `AssistantApp.http: HttpClient | None = None`.

- [ ] **Step 1: write failing tests** — fake http with `.get()`: `/credits` → `{"data":{"total_credits":100.5,"total_usage":25.75}}` → `74.75`; `/credits` raises (HTTP-status-like) → `/key` with `limit_remaining: 74.5` → `74.5`; both fail → `None`; `/key` with `limit_remaining: null` → `None`; slow `.get` (sleep) with `timeout=0.05` → `None`; balance reaches `collect_status` as `$74.75`, when `None` → «недоступен»; the api key never appears in the summary. Wiring: `build_assistant` sets `app.http` (extend an existing integration test assert).
- [ ] **Step 2: run** → FAIL (`openrouter_balance` not defined).
- [ ] **Step 3: implement** — `GET {OPENROUTER_BASE_URL}/credits` with `Authorization: Bearer`, each request under `asyncio.wait_for`; `/credits` failure → `/key`; `None` → «недоступен»; errors logged without response bodies.
- [ ] **Step 4: run** `uv run python -m pytest tests/test_status.py tests/test_assistant_app.py -v` → PASS.
- [ ] **Step 5: commit** `feat(assistant): openrouter balance in status`

### Task 3: `/status` and startup summary in `main.py`

**Files:**
- Modify: `assistant/main.py:76-106` (TurnRunner), `assistant/main.py:387-391` (run_bot)
- Modify: `tests/test_main.py`, `tests/test_assistant_app.py:192-206`

**Interfaces:**
- Consumes: `collect_status(app)` from Tasks 1–2.
- Produces: async command dispatch: `/status` → `await collect_status(...)`, the rest — the existing sync `_command_reply` via `asyncio.to_thread`.

- [ ] **Step 1: write failing tests** — `test_main.py`: monkeypatch `assistant.main.collect_status` → fixed text; the startup send contains it before `polling-start` (replaces the `pong` asserts); `TelegramError` on send → polling still starts; `collect_status` raising → the bot still starts (broad `except Exception` + warning). `test_assistant_app.py`: `/status` through the controller: the reply contains «Контекст» and «(оценка)», no row added to `turns`; `/new` unchanged.
- [ ] **Step 2: run** → FAIL.
- [ ] **Step 3: implement** — in `run()`: `command = await self._command(prompt)`, where `_command` returns `await collect_status(self.app)` for `/status`, else `await asyncio.to_thread(self._command_reply, prompt)`; the `/status` branch is removed from `_command_reply`. In `run_bot()`: `await send_text(app.bot, chat_id, await collect_status(app))` inside `try/except Exception` with `_LOG.warning("startup notification failed: ...")`.
- [ ] **Step 4: run** `uv run python -m pytest tests/test_main.py tests/test_assistant_app.py -v` → PASS.
- [ ] **Step 5: commit** `feat(assistant): /status summary replaces pong`

### Task 4: documentation

**Files:**
- Modify: `assistant/README.md` (`/status` section, ~lines 45–48)

- [ ] **Step 1: update** the README: summary contents, «недоступен» on failures, `pong` replacement; no new env vars.
- [ ] **Step 2: run** `uv run ruff check . && uv run python -m pytest` → clean; commit `docs(assistant): status summary`.
