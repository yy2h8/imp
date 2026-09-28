"""Bootstrap: probe the machine, render AGENTS.md, fingerprint in state.db.

Environment facts are pure code, never model-guessed ; the model only
adapts the Operating Manual in one tailoring turn afterwards.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from imp.agent import Agent, EventType
from imp.tools.ask import Ask

from .db import BUSY_TIMEOUT_MS, STATE_DB_NAME

_LOG = logging.getLogger(__name__)

TEMPLATE_NAME = "AGENTS.md.template"
ENV_BEGIN = "<!-- ENVIRONMENT:BEGIN -->"
ENV_END = "<!-- ENVIRONMENT:END -->"
# degradation path when the packaged template cannot be read (bootstrap
# failures degrade to a minimal manual rather than blocking startup).
# render_manual replaces everything between the markers with fresh facts.
MINIMAL_TEMPLATE = """# AGENTS.md

The assistant's operating manual. The packaged template was unreadable, so this
minimal manual was generated instead; delete the `fingerprint` key in
state.json and restart to retry.

<!-- ENVIRONMENT:BEGIN -->
<!-- ENVIRONMENT:END -->
"""

_PROBED_TOOLS = ("uv", "pip", "python3", "git", "curl", "wget")


def _tool_version(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        return "not found"
    try:
        result = subprocess.run(
            [name, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        version = (result.stdout or result.stderr).strip().splitlines()
        return f"{version[0]} ({path})" if version else path
    except (OSError, subprocess.SubprocessError):
        return path  # present but would not report a version


def _disk_free(path: Path) -> str:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return "unknown"
    return f"{usage.free / 1e9:.1f} GB free of {usage.total / 1e9:.1f} GB"


def _ram_total() -> str:
    try:
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return f"{int(line.split()[1]) // 1024} MB"
    except (OSError, ValueError, IndexError):
        pass
    return "unknown"


def _network() -> str:
    try:
        socket.create_connection(("api.telegram.org", 443), timeout=5).close()
    except OSError:
        return "unreachable"
    return "reachable"


@dataclass(slots=True, frozen=True)
class Probe:
    """Environment facts; also the fingerprint input."""

    os_name: str
    arch: str
    os_release: str
    kernel: str
    python_version: str
    python_path: str
    tools: dict[str, str]
    shell: str
    home: str
    cwd: str
    disk: str
    ram: str
    workspace: str
    has_openai_key: bool
    has_brave_key: bool
    network: str
    probed_at: str

    @classmethod
    def take(cls, workspace: Path) -> Probe:
        release = ""
        try:
            with open("/etc/os-release", encoding="utf-8") as fh:
                for line in fh:
                    if line.startswith("PRETTY_NAME="):
                        release = line.split("=", 1)[1].strip().strip('"')
                        break
        except OSError:
            pass
        shell = (
            str(Path("/bin/sh").resolve())
            if os.name == "posix"
            else os.getenv("COMSPEC", "cmd.exe")
        )
        return cls(
            os_name=platform.system() or "unknown",
            arch=platform.machine() or "unknown",
            os_release=release or "unknown",
            kernel=platform.release(),
            python_version=platform.python_version(),
            python_path=sys.executable,
            tools={name: _tool_version(name) for name in _PROBED_TOOLS},
            shell=shell,
            home=str(Path.home()),
            cwd=str(Path.cwd()),
            disk=_disk_free(workspace),
            ram=_ram_total(),
            workspace=str(workspace),
            has_openai_key=bool(os.getenv("OPENAI_API_KEY")),
            has_brave_key=bool(os.getenv("BRAVE_API_KEY")),
            network=_network(),
            probed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    def fingerprint(self) -> str:
        """Stable identity of the machine (timestamps and free-space noise
        excluded): a change means the manual's environment block is stale."""
        payload = json.dumps(
            {
                "os": self.os_name,
                "arch": self.arch,
                "release": self.os_release,
                "kernel": self.kernel,
                "python": self.python_version,
                "tools": self.tools,
                "ram": self.ram,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def render(self) -> str:
        tools = " · ".join(f"`{name}`: {value}" for name, value in self.tools.items())
        secrets = (
            f"`OPENAI_API_KEY={'yes' if self.has_openai_key else 'no'}`, "
            f"`BRAVE_API_KEY={'yes' if self.has_brave_key else 'no'}`"
        )
        return "\n".join(
            [
                f"- Host: `{self.os_name}/{self.arch}` {self.os_release}",
                f"- Kernel: `{self.kernel}`",
                f"- Python: `{self.python_version}` (`{self.python_path}`)",
                f"- Tools: {tools}",
                f"- Shell: `{self.shell}`",
                f"- Home: `{self.home}`",
                f"- Workspace (file-tool root): `{self.workspace}`",
                f"- Free disk: {self.disk} · RAM: {self.ram}",
                f"- Network at probe time: {self.network}",
                f"- Secrets present (presence only): {secrets}",
                f"- Probed at: `{self.probed_at}`",
            ]
        )


def render_manual(template: str, environment: str) -> str:
    """Splice the probed environment into the template's ENVIRONMENT block.
    Manual content outside the markers is preserved (hand edits survive
    re-bootstrap, per the template's own preamble)."""
    head, marker, tail = template.partition(ENV_BEGIN)
    _, end_marker, rest = tail.partition(ENV_END)
    if not marker or not end_marker:
        return template  # malformed template: keep it verbatim
    body = (
        f"{ENV_BEGIN}\n## Environment\n\n"
        "> Generated by bootstrap. Do not edit by hand; delete the `fingerprint` "
        "key in state.db and restart to regenerate.\n\n"
        f"{environment}\n{ENV_END}"
    )
    return f"{head}{body}{rest}"


def load_template(package_dir: Path) -> str:
    try:
        return (package_dir / TEMPLATE_NAME).read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read template: {exc}") from exc


def _state_connection(home: Path) -> sqlite3.Connection:
    home.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(home / STATE_DB_NAME, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA journal_mode=WAL")
    # Startup/bootstrap is synchronous (run in a worker thread); initialize
    # kv before the async composition root applies the remaining migrations.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    return conn


def read_state(home: Path) -> dict:
    """Read bootstrap/request state stored as JSON-valued rows in state.db."""
    conn = _state_connection(home)
    try:
        rows = conn.execute("SELECT key, value FROM kv").fetchall()
        state = {}
        for key, value in rows:
            try:
                state[key] = json.loads(value)
            except ValueError as exc:
                raise ValueError(f"Invalid state.db value for {key!r}") from exc
        return state
    finally:
        conn.close()


def write_state(home: Path, updates: dict) -> dict:
    """Merge JSON-valued state keys into the shared SQLite kv table."""
    conn = _state_connection(home)
    try:
        for key, value in updates.items():
            conn.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()
    state = read_state(home)
    return state


@dataclass(slots=True, frozen=True)
class BootstrapResult:
    """What a bootstrap run decided; drives the tailoring turn."""

    probe: Probe
    changed: bool  # manual written (first run, forced, or fingerprint mismatch)


def run_bootstrap(
    home: Path,
    package_dir: Path,
    probe: Probe | None = None,
    force: bool = False,
) -> BootstrapResult:
    """Probe → fingerprint check → render manual → record fingerprint .

    A mismatch means the stored manual describes a machine that no longer
    exists; the manual is rewritten with fresh facts (hand-edited manual text
    outside the ENVIRONMENT block survives via render_manual). A template
    failure degrades to a minimal manual rather than blocking startup.
    """
    probe = probe or Probe.take(home)
    path = home / "AGENTS.md"
    changed = (
        force
        or not path.exists()
        or probe.fingerprint() != read_state(home).get("fingerprint")
    )
    if changed:
        _LOG.info(
            "bootstrap: environment manual (re)generated from a fresh probe"
        )
        try:
            template = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            try:
                template = load_template(package_dir)
            except RuntimeError:
                template = MINIMAL_TEMPLATE
        manual = render_manual(template, probe.render())
        path.write_text(manual, encoding="utf-8")
        write_state(home, {"fingerprint": probe.fingerprint(), "tailored": False})
    return BootstrapResult(probe=probe, changed=changed)


def prune_scratch(home: Path, ttl_days: float) -> int:
    """Delete scratch/ files older than the TTL; returns how many went.

    Directories are never removed (the agent may nest work); pruning is
    best-effort — an undeletable file is left for the next boot.
    """
    scratch = home / "scratch"
    try:
        entries = list(scratch.iterdir())
    except OSError:
        return 0
    cutoff = time.time() - ttl_days * 86400
    removed = 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
                removed += 1
        except OSError:
            continue  # pruning is best-effort
    return removed


def needs_tailoring(state: dict) -> bool:
    """True when the Operating Manual still needs its one tailoring turn."""
    return not state.get("tailored")


async def tailor_manual(app, probe: Probe) -> None:
    """The single bootstrap tailoring turn : one agent turn that
    adapts the manual's Operating Manual section to the probed machine.

    Polling has not started, so this turn must not ask the owner questions.
    Shell access retains the service account's permissions.
    """
    prompt = (
        "Bootstrap tailoring turn. Adapt the Operating Manual section of "
        "AGENTS.md to this machine using ONLY the probed facts below — never "
        "invent facts. Preserve owner instructions and the generated environment "
        "block. This startup turn is non-interactive: do not ask questions or "
        "wait for replies. Keep every section except Operating-Manual adaptations; "
        "write the file back with str_replace. Adapt guidance like which "
        "package manager or venv tool to use (no uv → python3 -m venv + pip), "
        "disk and RAM budgeting (small disk → prune scratch aggressively), "
        f"and the shell to target.\n\nProbed environment:\n{probe.render()}"
    )
    write_state(home=app.config.workspace, updates={"tailored": False})
    _LOG.info("bootstrap: tailoring turn started (this can take a while)")
    agent = Agent(
        config=app.config,
        tools={
            name: tool for name, tool in app.agent.tools.items() if name != Ask.name
        },
        client=app.agent.client,
        context=app.agent.context,
    )
    completed = False
    async for event in agent.run_turn(prompt):
        if event.type is EventType.ERROR:
            raise RuntimeError(event.error_message or "Manual tailoring failed")
        if event.type is EventType.MODEL_RESPONSE:
            completed = bool(event.quote and event.quote.strip())
    if not completed:
        raise RuntimeError("Manual tailoring ended without a final response")
    write_state(home=app.config.workspace, updates={"tailored": True})
    _LOG.info("bootstrap: manual tailored")
