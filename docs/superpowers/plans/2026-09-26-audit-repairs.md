# Audit Repairs Implementation Plan

> **For agentic workers:** Use `superpowers:executing-plans` to implement this plan task-by-task in the current session. Steps use checkboxes for tracking. Do not dispatch agents unless separately requested.

**Goal:** Close every finding in AUDIT.md with a verified repair, an explicit accepted limitation, or a recorded deployment check.

**Architecture:** Keep imp's agent and the existing assistant modules. Use the existing JSON stores, one process, one owner, and stdlib synchronization. Delete unnecessary rendering and retry branches rather than introducing a framework, database, event bus, or new dependency.

**Tech Stack:** Python 3.12/3.13, uv, asyncio, existing OpenAI/httpx2 adapters, pytest, Ruff.

**Spec:** [AUDIT.md](../../../AUDIT.md), repository AGENTS.md, and the user decisions below.

## Execution status

Implemented code repairs and local verification: see [execution ledger](2026-09-26-audit-progress.md) and [current audit status](../../../AUDIT.md).
Final local checks: 362 tests on each of Python 3.12 and 3.13, Ruff, diff validation,
and isolated wheel smoke. Live Telegram/provider/target-host smoke checks remain
pending; they have not been replaced by mocks or marked complete.

## Global constraints and decisions

- Preserve the CLI behavior verified at `d0380e1`, except reproduced inherited defects explicitly covered here.
- Preserve `build_system_prompt(..., base_prompt=...)`, `FileSystemAdapter(..., skills_dir=...)`, and `SessionWriter(..., sessions_dir=...)` compatibility.
- Trusted host automation: auto-approved shell retains full service-account permissions. No new approval prompts or invented shell sandbox.
- Retain model-based manual tailoring and its existing non-interactive retry behavior.
- Scheduled jobs cannot ask questions. Waiting interactive requests survive restart; interrupted execution is reported without automatic replay.
- No new runtime dependencies. Env-only configuration. Blocking filesystem operations run off the event loop.
- Existing uncommitted fixes are the starting point, not work to redo. Baseline: 323 tests pass; Ruff and diff checks pass. No live Telegram/provider verification yet.
- README roadmap items (session resume, exact token counting) and new multimodal input are outside this repair pass.

Recommended remaining product choices, to be reviewed with this plan:

| Area | Planned behavior | Deliberate limitation |
|---|---|---|
| Identity | Exactly one positive owner ID, private chat only; keep existing env name | No multi-user routing |
| Output | Plain text, lossless bounded chunks through one delivery path | Markdown syntax remains visible; no rich renderer |
| Execution | One app execution lock for interactive turns and jobs | A long turn or unanswered question delays jobs |
| Commands | Exact `/new` and `/status` tokens; commands queue during active turns | No new cancellation command or automatic question timeout |
| Recurrence | First run after one interval; subsequent runs one interval after completion | No catch-up bursts or fixed-rate scheduling |
| Job interruption | Persist running state before work; recover as interrupted/error | Never infer that interrupted external actions are safe to repeat |
| Delivery failure | Preserve transcript/result and report failure; never rerun actions to retry delivery | No durable delivery service; ambiguous network retries can duplicate messages |
| Uploads | Persist attachment descriptors in the existing FIFO; process in its worker | No separate upload worker or concurrency pool |

## Review focus

1. An upload immediately followed by an answer must not block question routing: Task 4.
2. Cancellation/replacement with identical job contents must not be undone by old completion: Task 5.
3. Crash after side effects but before success persistence must not replay work: Tasks 4–5.
4. Emoji, giant code lines, and partial delivery must preserve content and expose failure: Task 3.
5. Cancellation between tool execution and result rendering must leave valid next-turn context: Task 6.

## Implementation sequence

Each task gets a focused regression, minimal implementation, passing targeted checks, and a reviewable diff. Reuse existing fixtures; use events and controlled clocks rather than timing sleeps. Commit only task-owned changes when committing is requested; do not sweep the existing working tree into an unrelated commit.

### Task 1 — Make installation and basic configuration truthful (A1, A18)

**Files:** `pyproject.toml`, `uv.lock`, `.github/workflows/ci.yml`, `imp/config.py`, `assistant/config.py`, `assistant/app.py`, `imp/adapters/session.py`; `tests/test_config.py`, `tests/test_assistant_config.py`, `tests/test_session.py`; README and AGENTS Python requirements.

**Interfaces:** Keep existing config fields; add backward-compatible `Config.from_env(*, workspace: Path | None = None)` so the assistant supplies its workspace before validation. SessionWriter signature stays unchanged.

- [ ] Add regressions: default relative SessionWriter path is `workspace/.imp/sessions`, custom relative path joins once, absolute custom path stays absolute; assistant ignores irrelevant invalid IMP_WORKSPACE; IDs must contain exactly one positive value; status size is 1..4096 and scratch TTL is positive. Preserve existing finite positive time/threshold checks.
- [ ] Run `uv run --frozen python -m pytest tests/test_config.py tests/test_assistant_config.py tests/test_session.py -q`; confirm new defects fail before repair.
- [ ] Explicitly package `imp` and `assistant`, including `AGENTS.md.template`; regenerate the lock for declared Python >=3.12. Fix path joining and config at their shared boundaries. Give the assistant an explicit `openai/gpt-5-mini` default without changing the CLI default; allow OPENAI_MODEL override.
- [ ] Add wheel-only smoke verification to CI: build, install wheel into an isolated environment, change outside checkout, import both packages, read template through importlib.resources, run `imp --help`. No editable install or source-path fallback.
- [ ] Verify frozen installs and tests separately on 3.12 and 3.13; update Python docs to >=3.12. Record unavailable interpreter/platform checks as pending, never passed.

### Task 2 — Enforce the intended owner boundary (A8, A10)

**Files:** `assistant/main.py`, `assistant/app.py`, `assistant/config.py`, `assistant/AGENTS.md.template`, `assistant/README.md`; `tests/test_assistant_app.py`, `tests/test_assistant_config.py`.

**Interfaces:** PollLoop and TurnRunner keep their public entry points. Use one exact command-token parser in both routing and command execution.

- [ ] Regressions: an owner message in a group, another user's private message, and malformed chat data cannot trigger uploads/tools; `/newsletter` is an ordinary prompt; `/new` and `/status` never answer `ask`; queued `/new` resets only when its turn arrives.
- [ ] Run `uv run --frozen python -m pytest tests/test_assistant_app.py tests/test_assistant_config.py -q` and establish failures.
- [ ] Require sender ID and private chat ID to equal the configured owner; parse exact first command token. Remove `min(ids)` as a routing policy. Remove the manual's unimplemented promise to proceed when no answer arrives.
- [ ] Replace false sandbox claims in comments/manual with actual file-adapter confinement and full shell service-account access. Keep no-ask jobs and model tailoring unchanged.
- [ ] Rerun focused tests and Ruff on changed Python files.

### Task 3 — One dependable Telegram delivery path (A5, A12, A18)

**Files:** `assistant/adapters/telegram.py`, `assistant/adapters/ui.py`, `assistant/app.py`, `assistant/main.py`, `assistant/scheduler.py`, `assistant/uploads.py`; `tests/test_telegram.py`, `tests/test_ui.py`, `tests/test_assistant_app.py`, `tests/test_scheduler.py`, `tests/test_send_file.py`.

**Interfaces:** `send_message(chat_id, text) -> int` sends one bounded plain-text message or raises TelegramError. Add `send_text(chat_id, text) -> list[int]` for chunked required delivery. Keep cosmetic edit/typing failures nonfatal. `send_document` raises on failure; the send_file tool converts it into ToolResult failure.

- [ ] Test lossless chunking with fences, underscores, blank lines, 4096+ single lines and non-BMP emoji: concatenated chunks equal input, each chunk fits a conservative 4096 UTF-16-unit budget, no surrogate is split. Test failed second chunk is surfaced, not successful completion.
- [ ] Test questions that fail to send clear their future instead of waiting; scheduled answers use all chunks; final delivery failure preserves transcript and produces observable failure.
- [ ] Mock text and multipart requests: JSON retry_after honored, 400 attempted once, transient retries bounded, no final sleep, token URLs absent from errors. Test “message is not modified” as harmless cosmetic feedback.
- [ ] Run `uv run --frozen python -m pytest tests/test_telegram.py tests/test_ui.py tests/test_assistant_app.py tests/test_scheduler.py tests/test_send_file.py -q` and confirm failure cases.
- [ ] Delete sanitizer/table rewriting/fence reconstruction and transport truncation. Put the small lossless splitter beside shared delivery. Route questions, final answers, errors and scheduled results through it. Keep status output deliberately capped.
- [ ] Share one private retry loop for JSON/multipart requests, rebuilding multipart requests per attempt. Retry only transient failures. Honor Telegram's requested wait even above the ordinary exponential-backoff cap; cancellation interrupts waiting. Log sanitized failures, not full request URLs.
- [ ] Remove `_pending`, unreachable creation branches and misleading debounce claims. Keep event-driven throttling plus forced final flush; fix `_tail` or replace it with the already-used first-line behavior. No background debounce task.
- [ ] Rerun focused tests and changed-file Ruff. Document ambiguous sends may duplicate, and stored transcript is the recovery source.

### Task 4 — Finish durable intake without blocking polling (A3, A9, A10)

**Files:** `assistant/main.py`, `assistant/bootstrap.py`, `assistant/uploads.py`; `tests/test_assistant_app.py`, `tests/test_bootstrap.py`, `tests/test_uploads.py`.

**Interfaces:** Extend the existing `pending_requests` FIFO to accept validated text requests and attachment descriptors. Read existing string entries for compatibility; no second queue/store. Active-request marker covers both attachment processing and agent work.

- [ ] Regression: block a fake attachment download, then deliver a text answer to an active question; polling still routes the answer. Restart with waiting attachments resumes them in order; active attachment work is reported, never replayed automatically.
- [ ] Test failed recovery notice preserves the active marker; malformed queue entries fail startup; cancellation during state writes joins the write; a question ending during persistence cannot silently discard the incoming message.
- [ ] Run `uv run --frozen python -m pytest tests/test_assistant_app.py tests/test_bootstrap.py tests/test_uploads.py -q` and confirm gaps.
- [ ] Persist only needed attachment fields with the cursor before download/STT. Perform uploads in the FIFO worker, keeping uploads separate from answers. Preserve active markers until completion and record failures without action replay. Route upload-only acknowledgments through required delivery.
- [ ] Keep state writes atomic and serialized. Do not add a generic persistence framework, unbounded background tasks, or claim exactly-once actions. Revalidate queue state before startup tailoring can run tools.
- [ ] Rerun focused tests; retain all already-passing FIFO/restart regressions.

### Task 5 — Make scheduled execution safe and predictable (A2, A4, A6, A11)

**Files:** `assistant/jobstore.py`, `assistant/scheduler.py`, `assistant/tools/schedule.py`, `assistant/app.py`, `assistant/main.py`; `tests/test_schedule.py`, `tests/test_scheduler.py`, `tests/test_assistant_app.py`.

**Interfaces:** Add `AssistantApp.execution_lock: asyncio.Lock` shared by the interactive worker and scheduler. Job adds persisted revision/run identity and execution-result metadata with backward-compatible defaults. `save_job` stays the common persistence boundary; add one store operation for conditional completion rather than read/compare/write at callers.

- [ ] Controlled-clock regressions: schedule every=60 at t=0, first due at t=60 after reload, finish t=75 gives next t=135; migrate old deadline-less jobs once. New earlier jobs are observed within IDLE_POLL_S (30 seconds), plus active execution time.
- [ ] Test JSON `[]`, bad schemas/intervals, and ID/filename mismatch alongside a valid due job; invalid files produce sanitized diagnostics without starving valid jobs.
- [ ] Test cancellation and same-ID replacement during an awaited model call, including identical contents; old completion cannot overwrite the replacement. Cancellation prevents future runs, does not promise to undo in-flight actions.
- [ ] Test job/interactive mutation never overlaps, ask replies still arrive under the execution lock, and a queued request does not acquire an active marker before acquiring the execution lock.
- [ ] Test persisted running job on startup becomes interrupted/error; failed result delivery leaves execution result/transcript and delivery failure visible, without re-execution. Test successful recurrence with failed delivery remains fixed-delay.
- [ ] Run `uv run --frozen python -m pytest tests/test_schedule.py tests/test_scheduler.py tests/test_assistant_app.py -q` and confirm failures.
- [ ] Persist first deadlines at creation and legacy initialization; cap scheduler sleeps at IDLE_POLL_S, delete MAX_SLEEP_S. Use completion time for recurrence and log unexpected scheduler failures instead of silently looping.
- [ ] Serialize complete model/tool turns with execution_lock. Polling and answer delivery never take it. Re-read the chosen job after acquiring it. Preserve no-ask job registry. Mark running durably before execution; store revision/run identity so completion is conditional on the claimed version.
- [ ] Protect job-store read/compare/replace with a single process-local lock inside synchronous store operations, called through to_thread; no awaits inside the critical section. Use unique temporary files and atomic replacement. Document one bot process per home; external manual job edits unsupported.
- [ ] Persist result and transcript reference separately from delivery outcome. Recovery reports interrupted runs without replaying them; manual rescheduling is the recovery action. No new delivery queue or automatic destructive retries.
- [ ] Rerun focused tests and changed-file Ruff; document fixed-delay scheduling, bounded detection latency and lock-related delays.

### Task 6 — Close resource lifetimes and repair interrupted context (A13, A17, A18)

**Files:** `assistant/app.py`, `assistant/main.py`, `imp/agent/agent.py`, `imp/agent/executor.py`, `imp/tools/shell.py`; `tests/test_assistant_app.py`, `tests/test_agent.py`, `tests/test_tools.py`.

**Interfaces:** Preserve Agent.run_turn and ToolResult. Cancellation must propagate after child tasks stop and protocol state is made replayable.

- [ ] Test context-manager exit closes the current writer after reset, including construction/turn failures; all owned clients close. Keep existing tool cancellation regression.
- [ ] Test cancellation and consumer/rendering failure mid-batch followed by another turn: no orphan tasks or unmatched function calls. Include cancellation after one read finishes and before a write starts; never falsely label an interrupted mutation as having made no changes.
- [ ] Test shell exit 3 returns ok=False while preserving output and exit status.
- [ ] Run `uv run --frozen python -m pytest tests/test_assistant_app.py tests/test_agent.py tests/test_tools.py -q` and establish failures.
- [ ] Close the current session in build_assistant finally, not the original session variable. Explicitly close owned async generators on consumer exit. Append real completed results and explicit interrupted/unknown results for outstanding calls in call order; never rerun tools to repair protocol history.
- [ ] Fix shell success at the shared tool boundary. Preserve already-working cancel/join shutdown behavior; test shell process-group cleanup without changing intended host permissions.
- [ ] Rerun focused tests and changed-file Ruff.

### Task 7 — Validate model output before executing it (A17)

**Files:** `imp/agent/model.py`, `imp/entities.py`, `assistant/main.py`, `assistant/scheduler.py`, `assistant/bootstrap.py`; `tests/test_agent.py`, `tests/test_entities.py`, `tests/test_assistant_app.py`, `tests/test_scheduler.py`.

**Interfaces:** Keep ModelReply and event contract; output validation happens before appending function calls or dispatching tools.

- [ ] Tests: multiple text items retain ordered text, refusal-only output is visible, incomplete/failed responses expose their reason, duplicate call IDs (within response or replay history) execute no tools. Test empty final response cannot reuse earlier commentary in interactive, scheduled, or tailoring turns.
- [ ] Run `uv run --frozen python -m pytest tests/test_agent.py tests/test_entities.py tests/test_assistant_app.py tests/test_scheduler.py -q`; mocks include real response status fields.
- [ ] Validate explicit failure/incomplete statuses and malformed calls at the model boundary; preserve stateless reasoning replay. Treat omitted optional provider fields compatibly, but do not infer success from explicit failures. Update final-answer tracking on every model response, including empty output.
- [ ] Rerun focused tests and changed-file Ruff. Check provider contracts against official docs during implementation where SDK/local types leave ambiguity.

### Task 8 — Enforce filesystem policy and bound memory (A14, A16)

**Files:** `imp/adapters/filesystem.py`, `imp/tools/fs.py`, `assistant/uploads.py`, `assistant/jobstore.py`, `assistant/app.py`, `assistant/adapters/telegram.py`, `assistant/tools/send_file.py`, config/env documentation as needed; `tests/test_filesystem.py`, `tests/test_uploads.py`, `tests/test_send_file.py`, `tests/test_schedule.py`, `tests/test_telegram.py`.

**Interfaces:** Reuse FileSystemAdapter resolution/denial policy for model-accessible paths. Add only small adapter operations needed for exclusive binary creation and bounded reads. Infrastructure transcripts/state remain infrastructure-owned.

- [ ] Regressions: dangling upload symlink, outside-home inbox/jobs/skill link, unsafe fallback attachment filename, collision during creation; no outside file is read or modified.
- [ ] Regressions: huge single line, paged read from a large file, oversized replace/send, download with false/missing Content-Length exceeding the cap; allocations stay bounded and partial files are cleaned up.
- [ ] Run `uv run --frozen python -m pytest tests/test_filesystem.py tests/test_uploads.py tests/test_send_file.py tests/test_schedule.py tests/test_telegram.py -q` and confirm failures.
- [ ] Validate every discovered SKILL.md and job path through existing confinement rules. Create upload destinations exclusively, retrying name collisions without following dangling links. Use the same policy for job storage while preserving atomic writes.
- [ ] Use bounded line/chunk reading rather than readlines. Bound full-file replacement and file transfers using existing max_http_bytes as the initial shared byte ceiling (10,000,000 default); document its broader I/O use instead of adding several new knobs. Reject oversized full replacements and uploads with actionable errors; cap actual downloaded bytes, not just headers/stat.
- [ ] Move remaining bounded blocking I/O to threads. Document shell output disk/process limits as service-account/deployment limits; do not promise Python tools constrain arbitrary host commands.
- [ ] Rerun focused tests and changed-file Ruff.

### Task 9 — Refresh prompts and remove misleading documentation (A7, A8, A15, A18)

**Files:** `imp/agent/prompt.py`, `imp/adapters/filesystem.py`, `assistant/app.py`, `assistant/scheduler.py`, `assistant/bootstrap.py`, `assistant/prompt.py`, `assistant/AGENTS.md.template`, READMEs, AGENTS.md, env examples, `assistant/deploy/*`, `Dockerfile` documentation; `tests/test_prompt.py`, `tests/test_filesystem.py`, `tests/test_bootstrap.py`, `tests/test_assistant_app.py`, `tests/test_scheduler.py`.

**Interfaces:** Carry actual workspace-relative skill paths independently of frontmatter names, preserving existing callers. Reuse one assistant prompt builder for startup/reset/jobs; no prompt service class.

- [ ] Tests: skill directory differs from metadata name and is still readable via the advertised path; `/new` sees changed AGENTS/skills; a later turn/job sees current time; shell probe describes the shell actually used, not the Python process.
- [ ] Run `uv run --frozen python -m pytest tests/test_prompt.py tests/test_filesystem.py tests/test_bootstrap.py tests/test_assistant_app.py tests/test_scheduler.py -q` and establish failures.
- [ ] Rebuild assistant prompt on reset and job start. Supply current time per turn without accumulating redundant system messages; remove stale startup-time instructions. Retain all manual preservation/tailoring tests.
- [ ] Probe the actual subprocess shell or state the configured executable without guessing its implementation. Remove nonexistent rebootstrap commands/spec references. Separate manual reset notice from automatic context-overflow notice.
- [ ] Correct service account/home setup, native dependency claims and Docker scope: current Dockerfile is the CLI image, not assistant deployment. Describe full service-account access and root consequences accurately without imposing a new isolation policy.
- [ ] Remove redundant URL validation only where HttpClient already enforces identical request/redirect policy; retain SSRF tests. Accept DNS resolution-to-connect races as an explicit residual limitation unless deployment requires a separate egress boundary; no custom DNS transport in this pass.
- [ ] Rerun focused tests and changed-file Ruff; review docs against actual commands.

### Task 10 — Verify the whole repaired branch and close the audit

**Files:** existing tests/fixtures, `.github/workflows/ci.yml`, `AUDIT.md`, `assistant/README.md`; no new test framework.

- [ ] Replace global scheduler constant mutation with monkeypatch, close fixture sessions/clients, import source constants, and replace flaky sleeps with events/controlled time in touched tests. Rename misleading Telegram test module only if it makes navigation clearer; add a small terminal rendering regression where core changes touch it.
- [ ] Run `uv run --frozen python -m pytest -q`, `uv run --frozen ruff check .`, and `git diff --check`; all must exit 0. Repeat full runs only after subsequent changes.
- [ ] Verify both Python matrix jobs, wheel-only smoke, and existing CLI help/turn/shell/cancellation behavior. Review diff against d0380e1 for accidental core rewrites.
- [ ] Prepare a concrete live smoke procedure: owner/private rejection, question/reply, long answer, file transfer, voice STT, scheduled first/second fire, stop/restart with waiting/running work, persisted manual edits, service install/restart on the intended host. Use harmless temporary files and test jobs; record delivery and transcript evidence.
- [ ] Before sending actual Telegram messages, making paid provider calls, or altering an installed service, obtain explicit scope/credentials if not already authorized. Missing access leaves these checks visibly unverified; it does not prevent finishing code repairs.
- [ ] Update each audit finding with status, actual regression names and evidence. Distinguish historical findings, repaired behavior and residual limits. Do not rewrite the verdict as production-ready based only on mocks.

## Audit coverage and completion rule

| Finding | Owning tasks |
|---|---|
| A1 packaging/Python | 1, 10 |
| A2 first recurrence | 5 |
| A3 message routing | Existing repair + 2, 4 |
| A4 non-interactive jobs/mutations | Existing no-ask repair + 5 |
| A5 formatting/delivery | 3 |
| A6 stale job completion | 5 |
| A7 bootstrap | Existing repair + 9 regression retention |
| A8 trusted host boundary | 2, 8, 9 |
| A9 durable intake | Existing repair + 4 |
| A10 identity/commands | 1, 2, 4 |
| A11 scheduler/recovery | 5 |
| A12 retry contract | 3 |
| A13 lifetime/cancellation | Existing repair + 6 |
| A14 filesystem paths | 8 |
| A15 skill/time/reset | 9 |
| A16 resource bounds | 8 |
| A17 shell/model output | 6, 7 |
| A18 smaller defects | 1, 3, 9, 10 |

Completion means every row has evidence or an explicitly accepted residual limit, not merely green tests. Known limits: one process/home, full host shell permissions, non-atomic external actions, potentially duplicate network sends, fresh context after restart, jobs delayed by active turns/questions, no automatic catch-up/replay, no new multimodal support, and live-platform checks only when actually performed.
