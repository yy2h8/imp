"""Plain-markdown Telegram status rendering with event-driven throttling."""

from __future__ import annotations

import time
from typing import Protocol

from imp.events import AgentEvent, EventType

from .telegram import TelegramError, send_text, split

_QUIET_TOOLS = {"read_file", "ask", "list_dir", "web_fetch", "web_search", "run_shell"}


class StatusBuffer:
    """Line buffer behind the turn's single status message: capped at max_chars
    with oldest lines dropped and a leading ``…``."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.lines: list[str] = []

    def append(self, line: str) -> None:
        self.lines.append(line)
        self._trim()

    def render(self) -> str:
        return "\n".join(self.lines)

    def _trim(self) -> None:
        while len(self.lines) > 1 and len("\n".join(self.lines)) > self.max_chars:
            self.lines.pop(0)
        if len(self.lines) == 1 and len(self.lines[0]) > self.max_chars:
            self.lines[0] = "…" + self.lines[0][-(self.max_chars - 1) :]

    def collapse(self, summary: str) -> None:
        """Replace the whole buffer with the one-line turn summary."""
        self.lines = [summary[: self.max_chars]]


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

    async def begin(self) -> None:
        """Create the status message up front, so the owner sees activity
        during the first (often long) model call; flushes then edit it."""
        if self.status_message_id is None:
            try:
                self.status_message_id = await self.bot.send_message(
                    self.chat_id, "…"
                )
                self._last_flush = time.monotonic()
            except TelegramError:
                pass  # cosmetic; the first flush retries creation

    async def handle(self, event: AgentEvent) -> None:
        if event.type is EventType.REASONING:
            self._add(self._tail(event.quote or "", 3))
        elif event.type is EventType.MODEL_RESPONSE:
            if event.quote:
                self._add(f"💭 {self._one_line(event.quote)}")
        elif event.type is EventType.TOOL_START:
            self._add(f"🔧 `{event.tool_name}` {self._args(event.tool_args)}".rstrip())
        elif event.type is EventType.TOOL_RESULT and event.tool_result:
            self._result(event)
        elif event.type is EventType.ERROR and event.error_message:
            self._add(f"*error:* {event.error_message}")

    def _result(self, event: AgentEvent) -> None:
        result = event.tool_result
        assert result is not None
        mark = "✓" if result.ok else "✗"
        if event.tool_name in _QUIET_TOOLS and result.ok:
            self._add(mark)
            return
        first_line = result.content.splitlines()[0][:200] if result.content else ""
        self._add(f"{mark} {first_line}".rstrip())

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
        if self.status_message_id is None:  # first reportable event: create now
            text = split(self.buffer.render())[0] if self.buffer.render() else ""
            if not text:
                return
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False
            self._last_flush = time.monotonic()
            return
        now = time.monotonic()
        if not force and now - self._last_flush < self.edit_interval:
            return
        text = split(self.buffer.render())[0] if self.buffer.render() else ""
        if not text:
            return
        ok = await self.bot.edit_message(self.chat_id, self.status_message_id, text)
        if ok:
            self._dirty = False
            self._last_flush = time.monotonic()
        elif force:
            # persistent edit failure: one fresh message, then stop editing
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False

    async def end_turn(self, summary: str) -> None:
        """Collapse the status to one line, then send the final answer as a
        new message (plain text, split within Telegram limits)."""
        self.buffer.collapse(summary)
        self._dirty = True  # collapse changed the text even if edits were current
        await self.flush(force=True)

    async def answer(self, text: str) -> None:
        await send_text(self.bot, self.chat_id, text)

    @staticmethod
    def _tail(text: str, limit: int) -> str:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return "\n".join(lines[-limit:])

    @staticmethod
    def _one_line(text: str) -> str:
        line = next((ln for ln in text.splitlines() if ln.strip()), "")
        return line[:200]

    @staticmethod
    def _args(args: dict | None) -> str:
        if not args:
            return ""
        rendered = " ".join(f"{k}={v}" for k, v in args.items()).replace("\n", " ")
        return rendered[:120] + ("…" if len(rendered) > 120 else "")
