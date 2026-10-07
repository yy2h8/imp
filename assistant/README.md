# assistant

A personal assistant reached over Telegram, built on `imp/` as a library. It
uses python-telegram-bot for Telegram transport and OpenRouter for model calls.

## What it does

- **One owner, one private chat.** Set exactly one positive user ID in
  `IMP_TG_ALLOWED_USER_IDS`; group chats and other senders are ignored.
- **Turn status.** A persistent `🧠 thinking…` message stays until the turn is
  over. While tools run, it shows only compact tool names and subjects in a
  short monospace log. The final status includes tool count, seconds, and the
  provider-reported USD cost when available. Reasoning text is never shown.
- **Markdown and files.** `telegramify-markdown` renders answers as Telegram
  entities and splits long replies safely. python-telegram-bot handles Bot API
  requests and uploads; the shared HTTP client streams downloads into sandboxed
  files with a byte cap. Documents, photos, videos, audio (including m4a),
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
- **Prompt context.** Before each interactive turn and scheduled run, the prompt
  includes the current manual, skills, memory, and a timestamp in `IMP_TZ` with
  its UTC offset and timezone name. The timestamp is a snapshot for that turn.
- **Self-configuring.** On first start it probes the machine and writes
  `<IMP_HOME>/AGENTS.md`, which shapes how it organises work (`scratch/`,
  `scripts/`, `projects/`, `outbox/`, `inbox/`). A model turn tailors the manual
  without asking questions. Owner edits survive refresh; startup corrects exact
  matches of obsolete default scheduling and plain-text reply guidance.
- **Scheduled jobs.** `schedule_job` supports one-shot `at`/`at_local`, interval
  `every`, and five-field `cron` in `IMP_TZ`. APScheduler persists schedules in
  `state.db`. Each job gets a fresh context and cannot use `ask`. Jobs can run
  concurrently with an interactive turn; one background job runs at a time by
  default. `IMP_MAX_CONCURRENT_JOBS` allows more parallel jobs. Results queue
  until the active interactive turn ends. Job deliveries and `list_jobs`
  include the run's tool/time/cost summary (`✓ done · N tools · M s · $X`).
- **Interactive FIFO.** Owner requests are accepted into a durable queue and
  run one at a time. Busy requests receive `Принято — в очереди …`. A request
  interrupted during execution is reported after restart and never replayed;
  waiting requests resume in order.
- **Telegram replies.** Replying to a prior message adds that message's text or
  caption as context to the new request. Replying while `ask` is pending answers
  that question directly.
- **Turn costs.** Each turn's API-reported tokens, USD cost, tool count,
  duration, and outcome are recorded in the database.
- **Commands.** `/new` starts a fresh conversation (the transcript remains in
  the database) and is refused while a turn is active. `/status` answers
  immediately, even mid-turn: local time in `IMP_TZ`, the running turn
  (prompt, elapsed time, tool count, current activity, latest model reply),
  a pending `ask`, running background jobs, queue depth, the latest chat
  response when idle, the three nearest scheduled jobs, the context-usage
  estimate (marked `~ … (оценка)`), and the OpenRouter
  balance in USD (wallet via a management key, else the key's remaining
  limit). `/cancel` aborts the active interactive turn, kills its process
  group, records the turn as cancelled, and lets the queue continue;
  scheduled jobs are never cancelled — `/status` surfaces them instead.
  A failed source is shown as «недоступен» without hiding the rest. Startup
  sends the same summary in place of `pong`; a failed startup notification
  never blocks polling.

## State and files

`<IMP_HOME>/state.db` is the assistant's single state artifact (SQLite WAL):

- `kv`: bootstrap fingerprint and tailoring state, plus Telegram redelivery
  deduplication. Polling advances the Telegram offset after intake completes,
  so accepted requests reach SQLite before Telegram acknowledges them.
- `queue`: waiting and active owner requests, plus a durable `collecting` row
  while an album is still arriving. That row blocks later requests to preserve
  FIFO order and resumes with received items after restart.
- `jobs_meta` and APScheduler's job table: job metadata, results, schedule state.
- `turns`: per-turn usage, cost, tools, duration, and outcome.
- `transcripts`: ordered JSON message rows, replacing `sessions/*.json`.
  Pruned on two TTL tiers — reasoning/tool rows drop after
  `IMP_TRANSCRIPT_ITEM_TTL_DAYS` (7), whole idle sessions after
  `IMP_TRANSCRIPT_TTL_DAYS` (90) — keeping user/assistant text searchable.
- `memory`: durable agent memory.

The agent still works with ordinary workspace files: `AGENTS.md`, skills,
`inbox/`, `outbox/`, `scratch/`, `scripts/`, and `projects/`. Bootstrap installs
bundled `brainstorming` and `ponytail` skills when their files are missing, and
never overwrites existing skill files; other skills can be added under `skills/`
as usual. SQLite contains assistant state and transcripts, not user documents
or workspace content.

The v2 assistant starts fresh. Existing v1 `state.json` and `jobs/*.json` are
left untouched and not imported; recreate any schedules with `schedule_job`.
To back up state, stop the bot and copy `state.db`.

## Manual updates and recovery

General behavior lives in `prompt.py`; tool usage lives in the tool definitions.
`AGENTS.md` holds workspace conventions and owner instructions. New installations
use the compact template. Existing manuals retain their custom content and are
not replaced wholesale; merge the compact `AGENTS.md.template` manually if desired,
preserving owner instructions and the generated environment block.

Bootstrap corrects known old scheduling and reply-format wording on the next
startup, including when the machine fingerprint has not changed. Customized
wording is left alone. Installed skills are also preserved; the bundled skill
edits apply to new installations or missing skill files.

If bootstrap tailoring failed, it retries at the next startup. To force a fresh
environment probe and tailoring pass, stop the bot, back up `state.db` and
`AGENTS.md`, delete only the `fingerprint` row from the `kv` table in `state.db`,
and restart. This preserves manual content outside the generated block; it does
not replace the manual with a new template.

## Running it

`assistant/` lives inside the imp repository and reuses its dependencies.

The assistant pins the OpenRouter base URL to `https://openrouter.ai/api/v1`;
the default model is `openai/gpt-5-mini`, overridden by `OPENAI_MODEL`. See
`.env.example` for every variable. Important settings are:

- `TELEGRAM_BOT_TOKEN` and `IMP_TG_ALLOWED_USER_IDS` (one owner ID).
- `OPENAI_API_KEY` (an OpenRouter key).
- `IMP_TZ` (timezone for `at_local` and cron; default `Asia/Almaty`).
- `IMP_MAX_CONCURRENT_JOBS` (background job cap; default `1`).
- `IMP_HOME` (state and file-tool root; shell commands run with the service's
  privileges).

## Configure and run with Docker

From the repository root:

```bash
cp assistant/.env.example assistant/deploy/assistant.env
# Set TELEGRAM_BOT_TOKEN and OPENAI_API_KEY in assistant/deploy/assistant.env.
docker build --target assistant -t imp-assistant .
docker compose -f assistant/deploy/compose.yaml run --rm assistant whoami
# Copy the printed owner ID into IMP_TG_ALLOWED_USER_IDS in assistant.env.
docker compose -f assistant/deploy/compose.yaml up -d
docker compose -f assistant/deploy/compose.yaml logs -f assistant
```

Docker sets `IMP_HOME=/data`; `state.db`, the manual, transcripts, and inbox/
workspace data persist in the named `assistant-data` volume. `whoami` uses
Telegram's `getUpdates` endpoint, so never run it alongside the bot. To run it
again after deployment, stop the service first:

```bash
docker compose -f assistant/deploy/compose.yaml stop assistant
docker compose -f assistant/deploy/compose.yaml run --rm assistant whoami
```

## Configure and run on a dedicated Linux machine

Supported hosts are glibc Linux on Python 3.12 or newer, including ARM64 SBCs
(the first target is Armbian). The systemd service runs as root. From a checkout
at `/opt/imp`, install uv system-wide and sync the runtime environment:

```bash
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
cd /opt/imp
uv sync --locked --no-dev
```

Configure the systemd environment file with the Telegram token and OpenRouter
key, then run `whoami` to discover the owner ID before enabling the service:

```bash
cp /opt/imp/assistant/.env.example /etc/assistant.env
# Set TELEGRAM_BOT_TOKEN and OPENAI_API_KEY.
chmod 600 /etc/assistant.env
set -a; . /etc/assistant.env; set +a
/usr/local/bin/uv run --locked --no-sync --directory /opt/imp python -m assistant whoami
# Put the printed ID in IMP_TG_ALLOWED_USER_IDS in /etc/assistant.env.
cp /opt/imp/assistant/deploy/assistant.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now assistant
```
The service starts at boot and restarts on failure. Follow logs with
`journalctl -u assistant -f`. `IMP_HOME` defaults to `~/assistant` under root's
home; it holds `state.db` and is the file-tool root. To run `whoami` again, stop
the service first.

## Security

**Execution boundary:** shell commands are approved automatically and run as
root, with root's filesystem, network, and environment access, including
credentials. `IMP_HOME` confines file tools, not shell commands. Run only one
bot process per home/token.

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
