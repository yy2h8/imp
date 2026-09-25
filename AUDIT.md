# imp + Telegram assistant audit — 2026-09-26

## Repair status — 2026-09-26

The code repairs below are implemented on the current working branch. The
original findings and reproductions are retained as historical evidence; this
status table supersedes their original “open” repair notes. No live Telegram,
OpenRouter/STT, Linux service or Raspberry Pi/musl verification has been performed,
so unattended deployment readiness is **not yet established**.

Final local verification: **362 tests passed on Python 3.12.11 and 3.13.15**;
`ruff check .` and `git diff --check` passed. Frozen Python 3.12 installation
passed. The final wheel was installed outside the checkout and both package
imports, the template, Unicode chunking and `imp --help` passed.

| Finding | Current disposition and regression evidence |
|---|---|
| A1 | Both packages/template included in wheel; lock matches Python >=3.12; isolated wheel imports/CLI help verified; CI includes wheel smoke. |
| A2 | Initial deadlines persist; two actual scheduler cycles tested by `test_first_and_second_fire_through_cycles`. |
| A3 | FIFO preserves early messages/uploads; `test_answer_cannot_jump_to_next_question_during_save` pins question identity across persistence. |
| A4 | No scheduled ask; execution lock serializes jobs and interactive turns. `test_execution_lock_keeps_waiting_request_durable` covers waiting claims. |
| A5 | Removed Markdown rewriting; lossless UTF-16-bounded plain-text chunks and explicit required-send failures. `test_split_preserves_arbitrary_content`, `test_partial_chunk_delivery_raises`. |
| A6 | Atomic revision-conditional completion prevents cancelled/replaced jobs being resurrected, including identical replacements. Scheduler regression tests cover both. |
| A7 | Model tailoring retained, non-interactive, retries failures, preserves manual edits. Existing bootstrap regressions retained. |
| A8 | Accepted trusted host execution; false confinement claims removed from manual, env and deployment docs. |
| A9 | Cursor/queue atomic writes; upload descriptors persisted before processing; worker independent of polling; malformed state stops before tailoring. Intake/restart tests in `test_assistant_app.py`. |
| A10 | Exactly one positive private-chat owner, exact command tokens, deferred commands; no question timeout or cancellation command promised. |
| A11 | Sleep capped at 30 seconds; malformed jobs isolated; running jobs recover as error without replay; fixed-delay recurrence; result/transcript/delivery-error persisted. |
| A12 | Shared multipart/JSON retry loop, JSON retry_after, permanent failures stop immediately, no final sleep, token URLs redacted. Telegram transport tests. |
| A13 | Current writer closes after reset; owned generators/tasks close on failure/cancellation; completed tool results retained, unknown interrupted outcomes explicit. `test_consumer_close_preserves_completed_result_and_next_turn`. |
| A14 | File adapter validates uploads/jobs/skill paths; exclusive upload creation handles dangling links. Filesystem/upload regressions. Filesystem races are not a host-security boundary. |
| A15 | Actual skill paths advertised; assistant prompts refresh on each turn/reset/job, including current time. Prompt/reset regressions. |
| A16 | Selected reads, replacement and transfers bounded; downloads enforce actual bytes; paged reads avoid whole-file buffering. Shell disk/process limits remain deployment responsibilities. |
| A17 | Nonzero shell exits fail; multiple text/refusal content retained; explicit incomplete responses and malformed/duplicate calls rejected before tools; empty final text cannot repeat commentary. Agent/tool regressions. |
| A18 | Relative sessions/config/default model/shell probe/deployment documentation corrected; dead UI state removed; touched fixtures close sessions and scheduler monkeypatch restores state. |

Simplification: no added runtime dependencies, no database or service framework;
removed the Markdown parser/fence repair, duplicate fetch validation, and held
message slot. Kept one execution lock and the existing JSON stores.

Accepted limits: one process per home/token; unanswered questions delay jobs;
no catch-up bursts; no automatic replay of interrupted external actions;
ambiguous sends can duplicate; no durable delivery retry service; no live session
resume or native multimodal input. File-tool/HTTP checks do not restrict the
trusted shell, eliminate filesystem races, or pin DNS between validation and
connection. See assistant/README.md for the remaining live smoke procedure.

## Original verdict and scope

Audited `95086c0` against the user-verified baseline `d0380e1`. The Telegram
assistant is a plausible prototype, but is **not ready for unattended use**.
The core agent was largely preserved; most new failures occur at boundaries
between polling, questions, scheduling, persistence, and delivery.

Read all tracked application source, all 20 test modules plus conftest, both
READMEs, the old audit, prompts/manual, env examples, packaging, CI, Dockerfile,
and service scripts. Reviewed dependency metadata and the baseline diff.
There are 3,999 application Python lines and 3,429 test lines.

Evidence labels below:

- **Reproduced:** exercised locally with temporary workspaces, fake credentials,
  scripted model replies, or HTTP mock transport.
- **Code path:** established by tracing the implementation; not a live incident.
- **Unverified:** needs the real provider, deployment target, or a product decision.

No real Telegram messages or paid model calls were made. The initial audit made
no production changes; subsequent repairs are recorded alongside the findings.
This report supersedes the previous AUDIT.md; its historical contents remain in Git.

## Verification results

| Check | Result |
|---|---|
| Python 3.13.15 / macOS full pytest suite | **306 passed, 1 failed**; `test_take_reports_live_facts` hardcodes Linux |
| `uv run ruff check .` | Passed |
| `python -m imp.cli --help` | Passed |
| `sh -n assistant/deploy/S99assistant` | Passed; syntax only, not a BusyBox deployment test |
| Build wheel with `uv build --wheel` | Builds, but archive includes **only `imp/`**, no `assistant/` |
| Original lock + `uv sync --frozen --python 3.12` | **Fails:** locked Python requirement is `>=3.13` |
| Original lock + `uv sync --frozen --python 3.13` | Passed |
| Telegram/model/STT live end-to-end | Not run |
| Raspberry Pi / musl deployment | Not run |

The initial non-frozen `uv run` regenerated the stale lockfile automatically.
That generated change was saved outside the repository for inspection, then the
original lockfile was restored and the frozen checks above were run. No lock
change is included in the audit diff. A successful non-frozen local run can mask
this CI failure.

The initial repair checkpoint passed 323 tests. Final repair verification is
recorded in the current status above and the execution ledger; the historical
installation failures in this section have since been repaired. Passing mocked
tests does not establish live service readiness.

## Does the previous audit make sense?

Partly. Its small-core architectural description is useful, but it does not
assess the Telegram addition and cannot serve as this branch's release check.

| Previous finding / claim | Reassessment |
|---|---|
| DNS rebinding bypass | Valid code-level limitation, inherited from baseline. DNS validation and connection resolution are separate. A comment documents it; it does not fix it. |
| Shell failures return `ok=True` | Valid and reproduced (`exit 3`). Still present in both UIs. |
| `read_file` reads the whole file | Valid, inherited. Also inspect `str_replace`, skill frontmatter, and outgoing file reads for memory limits. |
| Duplicate call IDs cause dictionary collision | **Stale:** both HEAD and `d0380e1` already use `id(call)` and have a regression test. The old claimed `KeyError` is also unsupported by a dictionary overwrite alone. Distinct provider call IDs remain a separate protocol concern. |
| Double URL validation | Valid redundant work. Keep validation at the shared HTTP boundary, including redirects. |
| `httpx` and quiet-tool documentation drift | Already corrected in README/AGENTS at the reviewed baseline; not a current finding. |
| Untyped skill tuples | Real maintainability nit, far below functional issues. Adding a type alone will not repair the assistant's wrong skill paths. |
| “Sandbox is done right once” / no abstraction leaks | Too broad. Shell is unrestricted; skill discovery reads unresolved paths; new uploads/job storage bypass FileSystemAdapter. |
| 154 tests prove health | Historical snapshot only. Tests mostly mock boundaries; several explicitly encode incorrect behavior. `.venv` is ignored, not committed. |

## Release blockers and high-impact defects

### A1 — Packaging and Python support disagree [P1, reproduced]

**Locations:** `pyproject.toml`, `uv.lock:3`, `.github/workflows/ci.yml`,
`assistant/README.md`.

Hatchling's default selection packages `imp/` only. The built wheel has no
`assistant/__main__.py`, modules, or manual template. Running from the repository
root hides this because the source directory is importable. A wheel-only install
cannot provide `python -m assistant`.

The project now declares Python >=3.12 and CI tests 3.12, but the committed lock
still requires >=3.13. The actual frozen 3.12 install failed before pytest.

**Repair:** explicitly package both directories (including the manual template),
regenerate the lock, test wheel imports from outside the checkout, and keep a
frozen 3.12/3.13 CI matrix. Update the contradictory Python requirement in AGENTS
and the root README if 3.12 remains supported.

### A2 — Newly scheduled recurring jobs never become due [P1, reproduced]

**Locations:** `assistant/tools/schedule.py:109`, `assistant/jobstore.py:113`,
`assistant/scheduler.py:47`.

`every` jobs are stored with no `last_run` or `next_run`. Every cycle computes
`now + every`, sleeps, reloads, and computes a new future deadline. A 60-second
job remained 60 seconds in the future at simulated offsets 0, 60, and 3600.
The tests verify this arithmetic once and invoke `run_job()` directly; neither
exercises first firing through the scheduler.

**Repair:** persist the first deadline when scheduling, initialize old pending
interval jobs once, and test successive scheduler cycles with controlled time.

### A3 — Messages are overwritten or consumed as answers to future questions [P1, reproduced]

**Locations:** `assistant/app.py:37`, `assistant/main.py:161`.

`AskRouter._held` is one string, not a queue. Two mid-turn messages leave only
the second. An upload placed through `hold()` does not resolve the current ask,
but `start()` later consumes that same upload as an answer to the next ask.
A text sent before any question exists is also silently treated as an answer to
a question the owner has not seen.

Reproductions: `deliver('first'); deliver('second'); take(); take()` returned
`second, None`; `hold('uploaded file prompt'); start().result()` returned that
file prompt. Existing tests assert the early-answer behavior rather than prove
that the owner's intent is preserved.

**Repair:** keep a FIFO of new prompts separate from the single pending question.
Only an actual pending question may consume an answer; uploads retain their
kind. Decide how explicit Telegram replies identify answers before supporting
more complex concurrent interaction.

**Implemented (2026-09-26):** replaced the held-message slot with a persisted
FIFO, independent of the current question. Early messages and uploads remain
new prompts; only text arriving during an active question can answer it.
`/new` and `/status` queue as commands instead of answering questions.

### A4 — Background jobs reuse interactive tools and question state [P1, code path]

**Locations:** `assistant/scheduler.py:79`, `assistant/app.py:140`,
`assistant/main.py:169`.

A fresh job Context is not execution isolation: jobs share the interactive tool
objects, workspace, client, and `prompt_user` closure. A scheduled `ask` creates
a future, but PollLoop routes incoming text to that future only while an
interactive `turn_task` is running. With no interactive turn, the answer starts
a new conversation turn and the scheduler remains stuck. During an interactive
turn, questions can instead consume the wrong turn's answers. The prompt lock
serializes questions, not their ownership. Mutating tools serialize only within
one batch, so jobs and interactive turns can modify the same file concurrently.

**Repair direction:** jobs should be non-interactive in the minimal design:
omit or explicitly refuse `ask`. Decide whether to serialize job execution with
interactive turns or introduce a shared mutation lock. Do not solve this by
adding unrelated per-chat session machinery.

**Decision and repair (2026-09-26):** the owner chose no questions for scheduled
jobs. The scheduler now excludes `ask` from its tool registry and instructs the
model to report missing essential information without waiting. Interactive tools
remain intact. A regression test exercises an attempted scheduled `ask`, verifies
it cannot invoke the question handler, and verifies interactive asking still
works. Job/interactive mutation coordination remains an open issue.

### A5 — Telegram formatting can silently discard answers [P1, reproduced + API contract]

**Locations:** `assistant/adapters/telegram.py:72`,
`assistant/adapters/ui.py:43`, `assistant/adapters/ui.py:75`,
`assistant/scheduler.py:74`.

All text is sent with legacy Markdown, including unescaped tool arguments,
questions, errors, and arbitrary model text. The sanitizer leaves ordinary
underscores/backticks untouched. Formatting failures are retried unchanged,
then become `None`; most callers ignore that result. A failed question send can
therefore leave the turn waiting forever for an unseen question.

The splitter adds closing/opening fences without reserving their space. Input
consisting of a fence and a 4096-character code line produced chunk lengths
`[8, 4104, 9]`; transport then truncates those chunks, corrupting content/fences.
The sanitizer also interprets headings/list markers inside code blocks and
changes the code itself. Scheduled answers bypass the splitter entirely and
are silently cut to 4096 characters.

**Repair direction:** plain text is the smallest dependable default. If rich
formatting is retained, use one tested rendering/delivery path with plain-text
fallback, strict chunk bounds, preserved code, and explicit send failures. Use
it for jobs and questions as well as interactive answers.

### A6 — A running job overwrites cancellation or replacement [P1, reproduced]

**Locations:** `assistant/scheduler.py:59`, `assistant/tools/schedule.py:109`,
`assistant/tools/schedule.py:152`.

While `_execute()` awaits the model, an interactive tool can cancel or replace
the same job. Completion unconditionally saves the stale Job object. Cancelling
a running recurring job in the reproduction left its stored status `pending`
after completion. Replacements are vulnerable to the same lost update.

**Repair:** compare the stored job with the version that began execution before
writing completion state. For the single-process design, keep comparison and
replacement in one serialized store operation. Define cancellation as preventing
future runs unless in-flight cancellation is explicitly added.

### A7 — Bootstrap can destroy edits, falsely succeed, or block startup [P1, mixed evidence]

**Locations:** `assistant/bootstrap.py:231`, `assistant/bootstrap.py:285`,
`assistant/main.py:212`.

- **Reproduced:** fingerprint changes reload the packaged template, not the
  existing manual. Owner edits outside ENVIRONMENT are lost despite the promise
  that they survive.
- **Reproduced:** tailoring ignores ERROR events and writes `tailored: true`
  after a failed model call.
- **Code path:** the fingerprint is saved before tailoring. A failure or process
  exit then leaves a matching fingerprint, and startup skips tailoring without
  consulting `needs_tailoring()`.
- **Code path:** tailoring has the ordinary `ask` tool, but polling has not
  started. One model question can block startup indefinitely.
- **Code path:** deleting AGENTS.md while keeping state does not regenerate it.

**Decision and repair (2026-09-26):** the owner chose to retain model-based
tailoring. Bootstrap now refreshes the existing manual rather than replacing it
with the packaged template, recreates missing manuals, and marks refreshes as
needing tailoring. Tailoring excludes `ask` and leaves its state pending on model
errors or an empty final response. Startup reports failure, continues with the
existing manual, and retries pending tailoring on the next start, even with an
unchanged fingerprint. The machine probe runs off the event loop. Malformed
environment markers preserve the original text rather than discarding it.
The model is instructed to preserve owner instructions; its edits still have
the trusted host access chosen for this assistant.

### A8 — Documented sandbox is not the actual execution boundary [P2, documentation fix]

**Locations:** `assistant/app.py:128`, `imp/tools/shell.py:79`,
`assistant/AGENTS.md.template`, `assistant/deploy/S99assistant`.

The assistant forces `auto_approve=True`. Shell sets only `cwd`; it inherits the
service account's filesystem access, network access, and environment, including
credentials. A harmless temporary marker outside the workspace was readable
through `run_shell`. `.env` filtering and SSRF validation do not constrain shell.
The BusyBox deployment recipe operates as root and does not drop privileges.

This is a documentation/deployment mismatch, not a request to invent a Python
shell sandbox. The older CLI already allowed host shell execution, but the new
bot removes approvals and advertises workspace confinement.

**Decision resolved (2026-09-26):** the owner explicitly chose trusted host
automation with full service-account access. Keep auto-approved shell execution;
do not add container requirements or approval prompts. Host access is intended,
not a defect to remove. Correct the manual and deployment docs to describe the
actual boundary and stop describing `send_file` as the only possible file egress.
The file tools should still enforce their own documented path restrictions.

## Additional functional and hardening issues

### A9 — Polling acknowledges work before it is durable [P2, code path]

**Locations:** `assistant/main.py:131`, `assistant/bootstrap.py:208`.

The offset for an entire batch is persisted before handling any of its updates.
Queued/running work is only in memory; a crash or restart loses accepted work.
Moving the offset write after `create_task()` alone would not make this durable.
`state.json` is rewritten in place, so interrupted writes can also lose the
cursor and replay messages. Valid JSON of the wrong type is accepted by
`read_state()`, and an invalid offset can prevent startup.

Queue draining occurs only after a long poll returns, so waiting prompts can
sit for roughly 25 seconds after a turn ends; new updates can overtake them.
Upload downloading and transcription are awaited inline in polling, delaying
answers and new updates further. Errors outside the narrow getUpdates handler
can terminate polling.

**Repair:** separate polling from queued turn processing; validate and atomically
replace state. Choose and document restart semantics (explicitly ephemeral
queue versus persisted inbox). Neither design gives exactly-once external side
effects without further work.

**Implemented (2026-09-26):** waiting prompts and the cursor are saved together
per update using atomic replacement. An independent worker drains the FIFO;
restart resumes waiting prompts in a fresh context and reports interrupted work
without replaying it. Invalid state stops startup instead of resetting the cursor.
Regression tests cover restart, duplicate updates, failed writes, and queue
draining during long polling. Upload processing still blocks polling; external
actions and Telegram delivery are not exactly-once.

### A10 — Single-owner identity and commands are inconsistent [P2, reproduced/code path]

**Locations:** `assistant/config.py:49`, `assistant/main.py:57`,
`assistant/main.py:148`, `assistant/main.py:200`.

Configuration permits multiple IDs, but every response and question goes to
`min(allowed_user_ids)`, and everyone shares one conversation. There is no chat
ID/type check. A group message from an allowed secondary user was accepted in
the reproduction. This allows incidental group activity to trigger private
assistant work and crosses users' conversation boundaries.

`/new` and `/status` sent during an ask are question answers; `/new` was
reproduced as the future's result. Prefix matching also turns `/newsletter`
into a reset. There is no cancellation command or question timeout. The manual's
promise to continue when no answer arrives is not implemented.

**Repair direction:** enforce one positive owner ID and their private chat,
parse exact command tokens, and keep control commands out of question answers.
Decide whether `/new` cancels a busy turn or is explicitly deferred.

### A11 — Scheduler responsiveness, corrupted jobs, and delivery status [P2, mixed]

**Locations:** `assistant/scheduler.py:38`, `assistant/jobstore.py:50`.

A future job makes the scheduler sleep up to an hour, so a newly inserted earlier
job can be late by almost an hour. Polling should have a short bounded interval
or a wake event. One malformed file containing JSON `[]` raises AttributeError
outside load_jobs' catch; reproduced. The outer scheduler hides the error and
retries forever, starving valid jobs; scheduling tools also fail while loading
the directory.

Jobs are marked done/rescheduled before result delivery, and failed sends are
ignored. A one-shot result can disappear permanently. Restart during execution
can instead rerun a job with already-performed side effects. Recurrence is based
on start time, so a job taking longer than its interval runs continuously.
These semantics need explicit tests and documentation.

**Repair:** validate JSON object/schema and report bad files; avoid stale long
sleeps; distinguish execution from delivery failure without automatically
repeating destructive work. Choose fixed-delay versus fixed-rate recurrence.

### A12 — Retry handling does not follow Telegram's error contract [P2, reproduced]

**Location:** `assistant/adapters/telegram.py:37` and `:107`.

Telegram returns flood-control delay in JSON `parameters.retry_after`; the client
reads only a header. A mocked JSON delay of 30 seconds caused a 1-second wait.
Permanent HTTP 400 formatting errors caused four identical requests and sleeps
of `[1, 2, 4, 8]`, including a useless sleep after the final attempt. Document
uploads return immediately on JSON 429/5xx rather than applying equivalent
retry rules. Ambiguous network retries can duplicate sends.

**Repair:** one shared retry policy for text/multipart, honor response parameters,
retry transient failures only, and avoid exposing token-bearing request URLs in
error text. Do not silently convert failed required delivery into success.
Telegram's contract: [ResponseParameters](https://core.telegram.org/bots/api#responseparameters).

### A13 — Resource and cancellation ownership is incomplete [P2, reproduced/code path]

**Locations:** `assistant/app.py:190`, `assistant/main.py:205`,
`imp/agent/executor.py:62`.

`build_assistant()` never closes the current interactive SessionWriter; the
file handle remained open after context exit in the reproduction. After reset,
cleanup must close the new session, not only the original local variable.
Shutdown cancels but does not await the scheduler, and does not explicitly
cancel/join the interactive turn before closing clients.

The inherited executor passes coroutines to `as_completed` without owning and
joining their tasks on cancellation. A waiting read tool continued after its
batch was cancelled in the reproduction. Early consumer failure can also leave
function-call items without outputs in the live Context, breaking later replay.

**Repair:** explicit task ownership and cancel/join cleanup before resource
closure. Define how an interrupted batch is completed in context or causes a
session reset; add cancellation tests before introducing a stop command.

**Partially implemented (2026-09-26):** shutdown now cancels and joins the
interactive worker and scheduler. The executor owns concurrent read-tool tasks
and cancels/joins them when the batch is cancelled, with a regression test.
SessionWriter closure and interrupted live-context repair remain open.

### A14 — Filesystem policy is bypassed in new paths [P2, reproduced/code path]

**Locations:** `assistant/uploads.py:107`, `assistant/jobstore.py:86`,
`imp/adapters/filesystem.py:140`.

Uploads construct and write Paths directly. `_target()` uses `exists()`, which
returns false for dangling symlinks; a pre-existing dangling `inbox/note.txt`
link wrote upload bytes outside inbox in a temporary-workspace reproduction.
A link can likewise target outside home. Job reads/writes also follow symlinked
paths without the filesystem adapter. Skill discovery validates only the skills
root, then reads each SKILL.md directly; an external symlink's frontmatter was
successfully read in the reproduction (inherited issue).

**Repair:** use the filesystem policy for model-accessible paths, reject unsafe
symlinks, and use exclusive upload creation. This improves adapter guarantees;
it does not constrain an unrestricted shell or eliminate all filesystem races.

### A15 — Assistant skill instructions and reset context are stale [P2, reproduced/code path]

**Locations:** `imp/agent/prompt.py:32`, `assistant/app.py:115`, `:173`.

The adapter discovers `<home>/skills`, but the generated prompt still instructs
`read_file('.imp/skills/<name>/SKILL.md')`; reproduced with a real discovered
skill. Metadata names can also differ from directory names in either UI.
Reset reuses the original system prompt, so changes to AGENTS.md, skills, and
the startup timestamp do not refresh on `/new`. Scheduled jobs also inherit
this old prompt, which is especially misleading for time-relative requests.

**Repair:** pass actual skill paths or a backward-compatible skills-directory
argument, rebuild prompt context on reset, and supply current time when a turn
or job begins. Preserve the existing public seams.

### A16 — Memory limits are applied too late [P2, code path]

**Locations:** `imp/adapters/filesystem.py:164`, `:224`,
`assistant/app.py:157`, `assistant/adapters/telegram.py:146`.

Text reads use `readlines()` before paging or executor truncation. Replace reads
the entire file too. `send_file` reads arbitrary-size files fully before upload,
and incoming downloads buffer the entire response. Limits are particularly
relevant to the documented low-memory Pi target. Shell tail capture bounds RAM
but its temporary output files can still exhaust disk; background descendants
can outlive a successfully exited shell.

**Repair:** explicit file/transfer bounds with actionable errors; bounded ranged
reads (including pathological single lines), outgoing size checks before read,
and streamed capped downloads. Full host resource enforcement belongs in the
chosen deployment boundary.

### A17 — Shell success reporting and model output edge cases [P2/P3, inherited]

**Locations:** `imp/tools/shell.py:110`, `imp/agent/model.py:31`,
`imp/entities.py:76`, `assistant/main.py:99`.

Nonzero shell exit returns `ok=True`; `exit 3` reproduced. Return failure while
preserving stdout/stderr/exit code for recovery.

Only the last message item supplies reply text, and refusal-only messages become
no text. Assistant TurnRunner retains the last *nonempty* MODEL_RESPONSE, so an
empty final output can re-send earlier commentary as the answer. Response
status/incomplete details are not checked. Existing fake responses omit these
fields, so they cannot establish live-provider behavior here.

Duplicate function-call IDs no longer collide internally, but are still replayed
as duplicates to the provider. Rejecting malformed output before executing tools
is safer than pretending that internal object identity repairs the wire protocol.

### A18 — Smaller concrete issues [P2/P3]

- **Reproduced regression:** `SessionWriter(Path('workspace'))` builds
  `workspace/workspace/.imp/sessions`. The new relative-path seam joins workspace
  twice when the default is used with a relative workspace. Normal Config paths
  are absolute, so the CLI avoids it.
- **Config:** status length and scratch TTL accept zero/negative values; status
  lengths above Telegram's limit are accepted. ID positivity is not checked.
  `Config.from_env()` validates IMP_WORKSPACE before the assistant replaces it,
  so an irrelevant CLI workspace can block bot startup.
- **Provider default:** assistant pins OpenRouter but inherits the unqualified
  `gpt-5-mini` default. Use a documented OpenRouter model ID or require it; live
  acceptance of this alias has not been tested.
- **Bootstrap facts:** `/proc/self/comm` identifies the Python process, not the
  subprocess shell. Template/rendered statements about ash are not a shell probe.
  Probe subprocesses/network checks originally blocked the event loop; A7 now
  runs the probe in a worker thread. Correct shell detection remains open.
- **Deployment docs:** systemd recipe does not create its required account/home.
  “Remaining dependencies are pure-Python” is false: the lock includes native
  `jiter` and `pydantic-core`. The checked Dockerfile copies only imp, so it is
  a CLI image, not an assistant deployment option as written.
- **UI maintenance:** `_pending` is set but does not schedule a delayed flush;
  `_tail` ignores its limit; a duplicate unreachable status-creation branch and
  inaccurate fallback comments obscure behavior. Remove dead state after choosing
  delivery semantics.
- **Test hygiene:** one scheduler test mutates IDLE_POLL_S globally without
  restoring it; several tests leak sessions/clients, hardcode constants, or use
  wall-clock sleeps. `test_ui.py` covers Telegram, not terminal rendering. Specs
  cited as “§...” / “D...” throughout the new code are not present in the repo.

## What is actually sound / not a finding

- The three core seams are narrow and mostly preserve CLI behavior. No rewrite
  of Agent/Context/entities is warranted just because the UI changed.
- Model state remains explicit; tool output ordering is deterministic within a
  completed batch; exceptions at execute_call become tool results.
- Non-owners are filtered by sender ID before upload processing. The problem is
  incomplete owner/chat policy, not unrestricted public access to the bot.
- STT is **not** proven broken: OpenRouter currently documents OpenAI-compatible
  multipart `/audio/transcriptions`, and its model catalog includes
  `openai/whisper-large-v3-turbo`. Existing SDK usage matches that contract.
  Live credentials/audio still need a smoke test. Sources:
  [STT contract](https://openrouter.ai/docs/guides/overview/multimodal/stt),
  [model catalog](https://openrouter.ai/api/v1/models?output_modalities=transcription).
- Saving a photo/PDF is not the same as giving the model vision/PDF input. The
  current model path accepts text and tool results only. Decide whether upload
  storage plus shell extraction is sufficient; do not silently add multimodal
  scope during hardening.
- Session resume and exact token counting are already roadmap items. They are
  not regressions introduced by Telegram and need not block this repair pass.

## Proposed repair sequence and brainstorming decisions

The objective is verification, hardening, and simplification of the existing
assistant while retaining the user-verified CLI core. No new framework, database,
or broad agent abstraction is needed for these repairs.

1. **Make the baseline honest:** packaging, lockfile, portable test, session path
   regression, shell exit status, config validation, correct docs.
2. **Make one-owner interaction dependable:** private chat identity, FIFO prompts,
   distinct pending questions, exact commands, shared delivery path, task cleanup.
3. **Make unattended work dependable:** persisted recurrence deadlines, responsive
   scheduler, cancellation/replacement protection, malformed-job isolation,
   non-interactive jobs, explicit delivery/restart semantics.
4. **Harden startup and clarify boundaries:** retain non-interactive model
   tailoring, preserve edits, refresh prompt/skills, bounded file I/O, coherent
   documentation of trusted host execution.
5. **Verify actual contracts:** wheel-only import outside the checkout; frozen
   installs for supported Pythons; regression tests that reproduce each repaired
   path; full pytest and ruff; then an explicit real Telegram/OpenRouter/STT and
   service-restart smoke test on the intended host.

Decisions to settle before behavior changes:

| Question | Smallest recommended choice | Alternative / cost |
|---|---|---|
| Who owns the conversation? | **Implemented:** exactly one owner, private chat only | Multiple users requires an explicit shared-session policy or isolated sessions |
| Can a message answer a question not yet sent? | **Implemented:** no; queue it as a new prompt | Correlation via explicit replies needs IDs and routing rules |
| May jobs ask questions? | **Decided and implemented: no `ask` for scheduled jobs** | Report missing essential information without waiting |
| Do jobs overlap interactive mutations? | **Implemented:** serialize execution for predictable workspace state | Shared mutation locking improves responsiveness but adds coordination |
| Is Markdown essential? | **Implemented:** plain text; preserve content and chunk correctly | Safe rich formatting with fallback adds parser/test surface |
| Must queued prompts survive restart? | **Decided and implemented: persist waiting prompts; report interrupted work without replay** | Resume in a fresh conversation; no exactly-once external-action guarantee |
| Does bootstrap need a model turn? | **Decided: retain model-based tailoring** | Hardened with no `ask`, preserved manual text, failure handling, and restart retries |
| What constrains shell execution? | **Decided: trusted host automation with full service-account access** | Keep auto-approval; correct sandbox claims; no new isolation requirement |

Regression tests should exercise complete boundaries rather than only helpers:
schedule through repeated cycles; multiple incoming messages during an ask;
job cancellation while awaiting a model; failed question/final-answer delivery;
restart with queued/running work; malformed stored JSON; long fenced answers;
bootstrap failure and manual preservation; cancellation followed by another turn.
