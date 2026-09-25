# assistant

A general personal assistant reached over **Telegram**, built on `imp/` as a
library. It keeps imp's capability set — read/write files, run shell, fetch and
search the web — and adds Telegram tools, a bootstrap step that writes its own
operating manual, and an internal scheduler for deferred work.

## What it is

- **One owner, one conversation.** Exactly one positive user ID in `IMP_TG_ALLOWED_USER_IDS` is
  served, in that owner’s private chat; groups and other senders are ignored.
- **Telegram as the interface.** Long-polling against the Bot API directly over
  `httpx2` — no Telegram framework dependency.
- **A single debounced status message** shows reasoning and tool activity while a
  turn runs, then collapses to a one-line summary. The final answer is its own
  message in plain text, split without changing its contents.
- **Tools:** imp's `list_dir` / `read_file` / `write_file` / `str_replace` /
  `run_shell` / `web_fetch` / `web_search`, plus `ask`, `send_file`,
  `schedule_job` and `unschedule_job`.
- **Self-configuring:** on first start it probes the machine and writes
  `<IMP_HOME>/AGENTS.md`, which shapes how it organises work (`scratch/`,
  `scripts/`, `outbox/`, `jobs/`, `inbox/`). A model turn tailors the manual
  without asking questions. Environment refreshes preserve the existing manual
  outside the generated block. Failed tailoring is reported and retried at the
  next startup; the bot continues with the existing manual.
- **Deferred work:** the agent schedules runs with the `schedule_job` tool call;
  an internal scheduler executes `jobs/*.json` — one-shot `at`/`at_local` or
  `every` interval — with a fresh context per run. Scheduled runs cannot use
  `ask`; include all necessary information in the job prompt. If essential
  information is missing, the job reports what prevented completion.
  The first interval deadline is persisted; subsequent intervals start at run
  completion. Jobs and interactive turns execute one at a time. Polling notices
  newly due jobs within 30 seconds, plus time spent waiting for active work or
  an unanswered question. Cancellation prevents future runs; it cannot undo
  actions already performed. Interrupted jobs become errors and require manual
  rescheduling. Job JSON stores the last result, transcript and delivery error;
  a failed send never causes execution to repeat.
- **Uploads and voice:** documents, photos and voice notes the owner sends land
  in `inbox/`; voice notes are transcribed (OpenRouter STT) into the next
  prompt, and the audio file is deleted after a successful transcription.
- **Request recovery:** waiting prompts are persisted in `state.json` and run in
  arrival order, independently of long polling. After restart they resume in a
  fresh conversation. A request interrupted during execution is reported and
  never automatically replayed: it may already have performed actions. Check
  its transcript before resubmitting. Corrupt state stops startup and requires
  repair; deleting it loses queued work and the saved Telegram cursor.
- **Questions:** only text received while a question is pending answers it.
  Earlier messages, uploads, `/new`, and `/status` remain queued requests.
- **Commands:** `/new` starts a fresh session (previous transcript stays on
  disk); `/status` reports context usage and the transcript name.

## Design

`AGENTS.md.template` is rendered by bootstrap into `<IMP_HOME>/AGENTS.md`;
`.env.example` lists every environment variable.

The assistant reuses imp's `Agent`, `Context`, `build_system_prompt`, `entities`,
`events`, `build_tools` and `Tool`, and builds its own composition root
and UI adapter. imp exposes three backward-compatible keyword-argument seams:
`build_system_prompt(..., base_prompt=...)`, `FileSystemAdapter(workspace,
skills_dir=...)`, `SessionWriter(workspace, sessions_dir=...)`.

## Layout

```
assistant/
├── main.py             app.py         config.py      prompt.py
├── bootstrap.py        scheduler.py   jobstore.py    uploads.py
├── adapters/  telegram.py  ui.py  stt.py
├── tools/     send_file.py  schedule.py   (ask is reused from imp)
└── deploy/    assistant.service  S99assistant
```

## Running it

`assistant/` lives inside the imp repository and reuses imp's dependencies, so
install imp as usual. Wheels include both packages and the manual template:

```bash
export TELEGRAM_BOT_TOKEN=...
export IMP_TG_ALLOWED_USER_IDS=123456789
export OPENAI_API_KEY=sk-or-...       # an OpenRouter key
export IMP_TZ=Asia/Almaty            # default; resolves at_local schedules
export BRAVE_API_KEY=...            # optional: enables web_search
uv sync                             # or: pip install . into a venv
uv run python -m assistant          # long-polling bot
python -m assistant whoami          # print sender IDs; Ctrl-C to exit
```

**Finding your Telegram id:** run `python -m assistant whoami` (only
`TELEGRAM_BOT_TOKEN` needed) **while the bot is stopped** — two `getUpdates`
consumers fight over the same update stream — send the bot any message, and its
sender id prints. Put that id into `IMP_TG_ALLOWED_USER_IDS` and start the bot.

The assistant talks to **OpenRouter only**: the base URL is pinned to
`https://openrouter.ai/api/v1`. imp's documented env vars (`OPENAI_MODEL`,
`IMP_MAX_CONTEXT`, `IMP_REASONING_EFFORT`, …) apply. The assistant model default
is `openai/gpt-5-mini`; OPENAI_MODEL overrides it. IMP_WORKSPACE is CLI-only;
the assistant uses IMP_HOME.

In a fresh chat the assistant answers `/status`; a busy bot replies with a
one-line `✓ done · N tools · X s` status before the answer.

Re-tailoring the operating manual (the old `--rebootstrap`): stop the bot,
delete the `fingerprint` key from `<IMP_HOME>/state.json`, start the bot.

See `.env.example` for every variable.

## Deploying

Targets a Raspberry Pi Zero 2W (musl/aarch64, Python 3.12, Dropbear, BusyBox)
and any generic Linux/Unix box with Python 3.12+.

**Install:** verify wheel availability on the target before deployment. `lxml`,
`jiter`, and `pydantic-core` include native components; this repository’s local
checks do not establish Raspberry Pi/musl compatibility. The root Dockerfile
builds the CLI image, not an assistant service image.

```bash
python3 -m venv /opt/imp-venv
/opt/imp-venv/bin/pip install /opt/imp     # the repo, scp'd to /opt/imp
```

**Transferring to the Pi:** Dropbear needs the legacy SCP protocol:

```bash
scp -O -r /path/to/imp root@pi-zero:/opt/imp
```

**Execution boundary:** the bot is trusted host automation. Shell commands are
approved automatically and inherit the service account’s filesystem, network
and environment access, including credentials. IMP_HOME confines file tools,
not shell commands. Run only one bot process per home/token. The BusyBox script
runs as its invoking account (normally root); the systemd unit uses `assistant`.
Systemd’s PrivateTmp and NoNewPrivileges settings still apply.

Before using the systemd unit, create its account and writable home (as an
administrator; adjust paths for your distribution):

```bash
useradd --system --create-home --home-dir /var/lib/assistant assistant
install -d -o assistant -g assistant /var/lib/assistant/assistant
# Set IMP_HOME=/var/lib/assistant/assistant in /etc/assistant.env.
```

**Autostart:**

- Generic Linux (systemd): `assistant/deploy/assistant.service` — copy to
  `/etc/systemd/system/`, put env in `/etc/assistant.env`,
  `systemctl daemon-reload && systemctl enable --now assistant`.
- Pi (BusyBox init): `assistant/deploy/S99assistant` — copy to
  `/etc/init.d/S99assistant`, `chmod +x`, env in `/etc/assistant.env`; it starts
  the bot at boot and supports `start|stop|restart|status`.

**BusyBox notes:** `run_shell` executes through `/bin/sh` on POSIX. The probe
records that executable’s resolved path; it does not guess the shell flavor. The init script above uses only BusyBox applets (`start-stop-daemon`,
`sh`, `sleep`).

## Development

The workflow mirrors imp's:

```bash
uv run ruff check .      # or: ruff check assistant/ tests/
uv run python -m pytest  # imp + assistant suites
```

imp's suite stays green; the three seams have one test each, and the assistant
modules are covered in `tests/test_bootstrap.py`, `tests/test_scheduler.py`,
`tests/test_schedule.py`, `tests/test_send_file.py`, `tests/test_telegram.py`,
`tests/test_stt.py`, `tests/test_uploads.py`, `tests/test_whoami.py`,
`tests/test_assistant_config.py` and `tests/test_assistant_app.py`.

## Limits and verification

`IMP_MAX_HTTP_BYTES` (default 10,000,000) also caps whole-file reads for
replacement, selected text reads, and file transfers. Use smaller line ranges
or a deliberate shell extraction for larger files. Downloads enforce actual
received bytes. File-tool path checks do not eliminate all filesystem races;
trusted shell commands can bypass them. Shell output capture uses temporary
disk files: disk quotas and descendant-process limits belong to the service
account/deployment, not this Python file adapter.

Required delivery failures are surfaced; transcripts and job results remain on
disk. Ambiguous network retries may duplicate messages. There is no exactly-once
external-action guarantee, persistent conversation resume, or automatic retry of
interrupted actions. Status messages are cosmetic, throttled on events, with a
forced flush at turn end. Commands queue while a turn is busy; questions have no
automatic timeout. Restart reports interrupted work and resumes waiting requests
in a fresh conversation. Uploads are saved/extracted as files; attaching an image
or PDF does not imply native multimodal model input.

Before calling a deployment ready, run these smoke checks with an explicitly
approved test owner/chat and provider budget:

1. Verify a private owner message works and a group/other sender is ignored.
2. Ask and answer a question; queue an upload and another task while waiting.
3. Deliver a long answer containing emoji/code and round-trip a small file.
4. Transcribe a short voice note using the configured OpenRouter STT model.
5. Schedule a harmless interval job and observe its first two runs; cancel it.
6. Restart with one active and two waiting requests: report the active request
   once without replay, then run waiting requests in order.
7. Restart during a harmless job: it becomes interrupted/error, never replayed.
8. Edit the manual, reset the conversation and restart; owner edits survive.
9. Verify service account/home, logs, native dependency installation, and stop/
   restart on the actual target host. No real credentials belong in test output.

Local mocks and wheel checks do not substitute for these live checks.
