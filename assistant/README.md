# assistant

A general personal assistant reached over **Telegram**, built on `imp/` as a
library. It keeps imp's capability set — read/write files, run shell, fetch and
search the web — and adds Telegram tools, a bootstrap step that writes its own
operating manual, and an internal scheduler for deferred work.

## What it is

- **One owner, one conversation.** Only user IDs in `IMP_TG_ALLOWED_USER_IDS` are
  served; everyone else is ignored.
- **Telegram as the interface.** Long-polling against the Bot API directly over
  `httpx2` — no Telegram framework dependency.
- **A single debounced status message** shows reasoning and tool activity while a
  turn runs, then collapses to a one-line summary. The final answer is its own
  message in Telegram-safe markdown.
- **Tools:** imp's `list_dir` / `read_file` / `write_file` / `str_replace` /
  `run_shell` / `web_fetch` / `web_search`, plus `ask` and `send_file`.
- **Self-configuring:** on first start it probes the machine and writes
  `<IMP_HOME>/AGENTS.md`, which shapes how it organises work (`scratch/`,
  `scripts/`, `outbox/`, `jobs/`).
- **Deferred work:** an internal scheduler runs `jobs/*.json` — one-shot `at` or
  `every` interval — with a fresh context per run.
- **Commands:** `/new` starts a fresh session (previous transcript stays on
  disk); `/status` reports context usage and the transcript name.

## Design

`AGENTS.md.template` is rendered by bootstrap into `<IMP_HOME>/AGENTS.md`;
`.env.example` lists every environment variable.

The assistant reuses imp's `Agent`, `Context`, `build_system_prompt`, `entities`,
`events`, `build_tools` and `Tool` unchanged, and builds its own composition root
and UI adapter. imp exposes three backward-compatible keyword-argument seams:
`build_system_prompt(..., base_prompt=...)`, `FileSystemAdapter(workspace,
skills_dir=...)`, `SessionWriter(workspace, sessions_dir=...)`.

## Layout

```
assistant/
├── cli.py            app.py         config.py      prompt.py
├── bootstrap.py      scheduler.py
├── adapters/  telegram.py  ui.py
├── tools/     send_file.py   (ask is reused from imp)
└── deploy/    assistant.service  S99assistant
```

## Running it

`assistant/` lives inside the imp repository and reuses imp's dependencies, so
install imp as usual and run the assistant from the repository root:

```bash
export TELEGRAM_BOT_TOKEN=...
export IMP_TG_ALLOWED_USER_IDS=123456789
export OPENAI_API_KEY=sk-...
export BRAVE_API_KEY=...            # optional: enables web_search
uv sync                             # or: pip install . into a venv
uv run python -m assistant          # long-polling bot
python -m assistant --probe         # print the environment probe and exit
python -m assistant --rebootstrap   # force re-probe + manual rewrite + tailoring
```

In a fresh chat the assistant answers `/status`; a busy bot replies with a
one-line `✓ done · N tools · X s` status before the answer.

See `.env.example` for every variable.

## Deploying

Targets a Raspberry Pi Zero 2W (musl/aarch64, Python 3.12, Dropbear, BusyBox)
and any generic Linux/Unix box with Python 3.12+.

**Install (both targets)** — no compilation on device: `lxml` ships
`musllinux_1_2_aarch64` wheels and the remaining dependencies are pure-Python.

```bash
python3 -m venv /opt/imp-venv
/opt/imp-venv/bin/pip install /opt/imp     # the repo, scp'd to /opt/imp
```

**Transferring to the Pi:** Dropbear needs the legacy SCP protocol:

```bash
scp -O -r /path/to/imp root@pi-zero:/opt/imp
```

**Autostart:**

- Generic Linux (systemd): `assistant/deploy/assistant.service` — copy to
  `/etc/systemd/system/`, put env in `/etc/assistant.env`,
  `systemctl daemon-reload && systemctl enable --now assistant`.
- Pi (BusyBox init): `assistant/deploy/S99assistant` — copy to
  `/etc/init.d/S99assistant`, `chmod +x`, env in `/etc/assistant.env`; it starts
  the bot at boot and supports `start|stop|restart|status`.

**BusyBox notes:** `run_shell` executes via the system shell, which is `ash` on
the Pi — the generated `AGENTS.md` records this so the agent does not assume
bash. The init script above uses only BusyBox applets (`start-stop-daemon`,
`sh`, `sleep`).

## Development

The workflow mirrors imp's:

```bash
uv run ruff check .      # or: ruff check assistant/ tests/
uv run python -m pytest  # imp + assistant suites
```

imp's suite stays green; the three seams have one test each, and the assistant
modules are covered in `tests/test_bootstrap.py`, `tests/test_scheduler.py`,
`tests/test_send_file.py`, `tests/test_telegram.py` and
`tests/test_assistant_app.py`.
