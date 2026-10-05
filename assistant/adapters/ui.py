"""Turn status rendering: a persistent thinking message, a tool-only live
log in a monospace block, and a one-line cost-aware collapse at turn end."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from imp.events import AgentEvent, EventType

from .telegram import TelegramError, send_text

STATUS_LINES = 8  # tool lines kept in the live log
MAX_SUBJECT_CHARS = 40

HEADER_THINKING = "🧠 thinking…"
HEADER_WORKING = "🧠 working"

_SHORT_NAMES = {
    "read_file": "read",
    "write_file": "write",
    "str_replace": "edit",
    "list_dir": "ls",
    "run_shell": "shell",
    "web_fetch": "fetch",
    "web_search": "search",
    "send_file": "send",
    "schedule_job": "sched",
    "unschedule_job": "cancel",
    "ask": "ask",
    "memory_set": "mem+",
    "memory_delete": "mem-",
    "memory_list": "mem?",
    "search_transcripts": "find",
    "cost_report": "cost",
    "queue_status": "queue",
}


def turn_summary(tools: int, seconds: int, ok: bool, cost_usd: float | None) -> str:
    """The one-line status collapse at turn end."""
    mark = "✓ done" if ok else "✗ failed"
    text = f"{mark} · {tools} tools · {seconds} s"
    if ok and cost_usd is not None:
        text += f" · ${cost_usd:.4f}"
    return text


def _clip(text: str, limit: int = MAX_SUBJECT_CHARS) -> str:
    line = " ".join(text.split())
    return line[:limit]


def tool_subject(name: str, args: dict | None) -> str:
    """One short subject per tool — paths, first command line, hosts — never
    string-argument blobs."""
    args = args or {}
    if name in {"read_file", "write_file", "str_replace", "list_dir"}:
        path = str(args.get("path", ""))
        if len(path) > MAX_SUBJECT_CHARS:
            path = Path(path).name
        return _clip(path)
    if name == "run_shell":
        command = str(args.get("command", ""))
        return _clip(command.splitlines()[0] if command else "")
    if name == "web_fetch":
        url = str(args.get("url", ""))
        return _clip(urlsplit(url).hostname or url)
    if name == "web_search":
        return _clip(str(args.get("query", "")))
    if name == "send_file":
        return _clip(Path(str(args.get("path", ""))).name)
    if name in {"schedule_job", "unschedule_job"}:
        return _clip(str(args.get("id", "")))
    if name == "ask":
        question = str(args.get("question", ""))
        return _clip(question.splitlines()[0] if question else "")
    if name in {"memory_set", "memory_delete"}:
        return _clip(str(args.get("key", "")))
    if name == "memory_list":
        return ""
    if name == "search_transcripts":
        return _clip(str(args.get("query", "")))
    if name in {"cost_report", "queue_status"}:
        return _clip(str(args.get("period", "")))
    return ""


def tool_label(name: str | None, args: dict | None) -> str:
    """One-line 'name subject' description for status displays."""
    short = _SHORT_NAMES.get(name or "", (name or "")[:8])
    return f"{short} {tool_subject(name or '', args)}".strip()


class StatusBuffer:
    """Line buffer for the tool log: capped at max_chars (oldest dropped) and
    max_lines (dropped lines counted in a leading ``… +N earlier``)."""

    def __init__(self, max_chars: int, max_lines: int = STATUS_LINES) -> None:
        self.max_chars = max_chars
        self.max_lines = max_lines
        self.lines: list[str] = []
        self.dropped = 0

    def append(self, line: str) -> None:
        self.lines.append(line)
        while len(self.lines) > self.max_lines:
            self.lines.pop(0)
            self.dropped += 1
        self._trim()  # char-cap trims are a deep safety net, not overflow

    def render(self) -> str:
        out = list(self.lines)
        if self.dropped:
            out.insert(0, f"… +{self.dropped} earlier")
        return "\n".join(out)

    def _trim(self) -> None:
        while len(self.lines) > 1 and len("\n".join(self.lines)) > self.max_chars:
            self.lines.pop(0)
        if len(self.lines) == 1 and len(self.lines[0]) > self.max_chars:
            self.lines[0] = "…" + self.lines[0][-(self.max_chars - 1) :]

    def collapse(self, summary: str) -> None:
        """Replace the whole buffer with the one-line turn summary."""
        self.lines = [summary[: self.max_chars]]
        self.dropped = 0


class _Transport(Protocol):
    def send_message(self, chat_id: int, text: str) -> object: ...

    def edit_message(self, chat_id: int, message_id: int, text: str) -> object: ...


class TelegramUIAdapter:
    """Renders imp's agent events to Telegram: one status message created
    eagerly at turn start and edited on a throttle, and the final answer as
    its own message(s). Typing is the turn runner's job (it must outlive
    individual events)."""

    def __init__(
        self,
        bot: _Transport,
        chat_id: int,
        edit_interval: float = 2.5,
        max_chars: int = 3500,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.edit_interval = edit_interval
        self.max_chars = max_chars
        self.buffer = StatusBuffer(max_chars)
        self.status_message_id: int | None = None
        self._last_flush = 0.0
        self._dirty = False  # text changed since the last successful edit
        self._saw_tool = False
        self._collapsed = False

    async def begin(self) -> None:
        """Create the status message up front, so the owner sees activity
        during the first (often long) model call."""
        if self.status_message_id is None:
            try:
                self.status_message_id = await self.bot.send_message(
                    self.chat_id, HEADER_THINKING
                )
                self._last_flush = time.monotonic()
            except TelegramError:
                pass  # cosmetic; the first flush retries creation

    async def handle(self, event: AgentEvent) -> None:
        if event.type is EventType.TOOL_START:
            self._saw_tool = True
            short = _SHORT_NAMES.get(event.tool_name or "", (event.tool_name or "")[:8])
            line = f"{short:<8}{tool_subject(event.tool_name or '', event.tool_args)}"
            self._add(line.rstrip())
        elif event.type is EventType.ERROR and event.error_message:
            self._add(f"*error:* {event.error_message}")
        # THINKING / REASONING / MODEL_RESPONSE never touch the status

    def _add(self, line: str) -> None:
        self.buffer.append(line)
        self._dirty = True

    async def flush(self, force: bool = False) -> None:
        try:
            await self._flush(force)
        except TelegramError:
            pass  # cosmetic status must never abort required answer delivery

    async def _flush(self, force: bool = False) -> None:
        """Edit the status message if the text changed and the throttle allows.
        The first flush creates the message right away; afterwards edits are
        throttled (unless forced). On persistent edit failure, sends a fresh
        status message once and stops editing for the rest of the turn."""
        if not self._dirty:
            return
        text = self._render()
        if self.status_message_id is None:  # first reportable event: create now
            if not text:
                return
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False
            self._last_flush = time.monotonic()
            return
        now = time.monotonic()
        if not force and now - self._last_flush < self.edit_interval:
            return
        ok = await self.bot.edit_message(self.chat_id, self.status_message_id, text)
        if ok:
            self._dirty = False
            self._last_flush = time.monotonic()
        elif force:
            # persistent edit failure: one fresh message, then stop editing
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False

    def _render(self) -> str:
        """Header line + the tool log fenced as one monospace block (the
        transport renders the fence as a Telegram pre entity)."""
        body = self.buffer.render()
        if not body:
            return ""
        if self._collapsed:
            return body
        header = HEADER_WORKING if self._saw_tool else HEADER_THINKING
        return f"{header}\n```\n{body}\n```"

    async def end_turn(self, summary: str) -> None:
        """Collapse the status to one line; the final answer follows as a
        new message."""
        self.buffer.collapse(summary)
        self._collapsed = True
        self._dirty = True  # collapse changed the text even if edits were current
        await self.flush(force=True)

    async def answer(self, text: str) -> None:
        await send_text(self.bot, self.chat_id, text)
