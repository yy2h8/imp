# assistant

A personal assistant reached over Telegram, built on `imp/` as a library. It
uses aiogram for Telegram transport and OpenRouter for model calls.

## What it does

- **One owner, one private chat.** Set exactly one positive user ID in
  `IMP_TG_ALLOWED_USER_IDS`; group chats and other senders are ignored.
- **Turn status.** A persistent `🧠 thinking…` message stays until the turn is
  over. While tools run, it shows only compact tool names and subjects in a
  short monospace log. The final status includes tool count, seconds, and the
  provider-reported USD cost when available. Reasoning text is never shown.
- **Markdown and files.** `telegramify-markdown` renders answers as Telegram
  entities and splits long replies safely. aiogram handles Bot API requests,
  uploads, and downloads. Documents, photos, videos, audio (including m4a),
  voice notes, video notes, animations, and stickers are accepted. Forwarded
  source details are retained, and media albums are merged into one request.
  Voice notes and playable audio are transcribed when STT is available.
- **Tools.** imp's `list_dir`, `read_file`, `write_file`, `str_replace`,
  `run_shell`, `web_fetch`, `web_search`, and `ask`, plus `send_file`,
  `schedule_job`, `unschedule_job`, `list_jobs`, `search_transcripts`,
  `cost_report`, `queue_status`, and durable memory tools (`memory_set`,
  `memory_list`, `memory_delete`).
- **Durable memory.** The assistant can save concise preferences and facts that
  survive `/new`, restarts, and scheduled runs. A capped digest is included in
  each system prompt. Values are limited to 2,048 characters; at most 200 keys.
- **Self-configuring.** On first start it probes the machine and writes
  `<IMP_HOME>/AGENTS.md`, which shapes how it organises work (`scratch/`,
  `scripts/`, `projects/`, `outbox/`, `inbox/`). A model turn tailors the manual
  without asking questions. Edits outside the generated block survive refresh.
- **Scheduled jobs.** `schedule_job` supports one-shot `at`/`at_local`, interval
  `every`, and five-field `cron` in `IMP_TZ`. APScheduler persists schedules in
  `state.db`. Each job gets a fresh context and cannot use `ask`. Jobs can run
  concurrently with an interactive turn and with other jobs (default maximum
  two); results queue until the active interactive turn ends.
- **Interactive FIFO.** Owner requests are accepted into a durable queue and
  run one at a time. Busy requests receive `Принято — в очереди …`. A request
  interrupted during execution is reported after restart and never replayed;
  waiting requests resume in order.
- **Turn costs.** Each turn's API-reported tokens, USD cost, tool count,
  duration, and outcome are recorded in the database. `/status` continues to
  show context estimate and the transcript ID.
- **Commands.** `/new` starts a fresh conversation (the transcript remains in
  the database); `/status` reports context use and transcript ID.

## State and files

`<IMP_HOME>/state.db` is the assistant's single state artifact (SQLite WAL):

- `kv`: bootstrap fingerprint and tailoring state, plus Telegram redelivery
  deduplication (Telegram update offset itself is managed by aiogram).
- `queue`: waiting and active owner requests.
- `jobs_meta` and APScheduler's job table: job metadata, results, schedule state.
- `turns`: per-turn usage, cost, tools, duration, and outcome.
- `transcripts`: ordered JSON message rows, replacing `sessions/*.jsonl`.
- `memory`: durable agent memory.

The agent still works with ordinary workspace files: `AGENTS.md`, skills,
`inbox/`, `outbox/`, `scratch/`, `scripts/`, and `projects/`. SQLite contains
assistant state and transcripts, not user documents or workspace content.

The v2 assistant starts fresh. Existing v1 `state.json` and `jobs/*.json` are
left untouched and not imported; recreate any schedules with `schedule_job`.
To back up state, stop the bot and copy `state.db`.

## Running it

`assistant/` lives inside the imp repository and reuses its dependencies.

```bash
export TELEGRAM_BOT_TOKEN=...
export IMP_TG_ALLOWED_USER_IDS=123456789
export OPENAI_API_KEY=sk-or-...       # an OpenRouter key
export IMP_TZ=Asia/Almaty             # default; used by at_local and cron
export BRAVE_API_KEY=...              # optional: enables web_search
uv sync
uv run python -m assistant
python -m assistant whoami            # run while the bot is stopped
```

See `.env.example` for every variable. The assistant pins the OpenRouter base
URL to `https://openrouter.ai/api/v1`; the default model is
`openai/gpt-5-mini`, overridden by `OPENAI_MODEL`. `IMP_HOME` is the file-tool
confinement root; shell commands run with the service account's permissions.
Set `IMP_MAX_CONCURRENT_JOBS` to change the job concurrency cap (default `2`).

## Deployment

Supported target: glibc Linux, including ARM64 SBCs. The first deployment
target is Armbian on ARM64. BusyBox/musl deployment is not supported.

**Docker:**

```bash
cp assistant/.env.example assistant/deploy/assistant.env  # fill in values
docker build --target assistant -t imp-assistant .
docker compose -f assistant/deploy/compose.yaml up -d
```

Docker keeps assistant files and `state.db` in the `/data` volume. The shell
and file tools operate inside the container and mounted volumes.

**Bare-metal systemd:** use `assistant/deploy/assistant.service`, create a
dedicated service account and writable home, place variables in
`/etc/assistant.env`, then enable the service. For example:

```bash
useradd --system --create-home --home-dir /var/lib/assistant assistant
install -d -o assistant -g assistant /var/lib/assistant/assistant
# Set IMP_HOME=/var/lib/assistant/assistant in /etc/assistant.env.
cp assistant/deploy/assistant.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now assistant
```

**Execution boundary:** shell commands are approved automatically and inherit
the service account's filesystem, network, and environment access, including
credentials. IMP_HOME confines file tools, not shell commands. Run only one bot
process per home/token.

## Development and smoke checks

```bash
uv run ruff check .
uv run python -m pytest
```

Before deploying, verify with an approved test owner/chat and provider budget:

1. Owner private messages work; other senders and groups are ignored.
2. Answer a pending `ask`; queue another text and an upload during a turn.
3. Share/forward an m4a as audio and as a document; both land in `inbox/`.
4. Share a multi-photo album and verify it produces one queued request.
5. Send a forwarded document and verify its source is represented in the prompt.
6. Deliver a long markdown answer with code and round-trip a small file.
7. Observe a turn summary with real USD cost when the provider reports it.
8. Schedule an interval and cron job while an interactive turn is running; verify
   jobs run concurrently and their results wait until the turn ends; cancel them.
9. Restart with one active and two waiting requests: the active request is
   reported once without replay, then waiting requests run in order.
10. Restart during a harmless job: it is marked interrupted and never replayed.

No exactly-once external-action guarantee, persistent conversation resume, or
automatic retry of interrupted actions. Uploaded images/PDFs are saved as files;
they are not passed as native multimodal model input.
