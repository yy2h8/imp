"""Telegram rendering: sanitise model markdown, split at 4096, one debounced
status message per turn with oldest-line dropping.

Rendering only — transport calls go through the injected TelegramBot.
"""

from __future__ import annotations

import re
import time
from typing import Protocol

from imp.events import AgentEvent, EventType

# Telegram's hard message limit
MAX_MESSAGE_CHARS = 4096

_QUIET_TOOLS = {"read_file", "ask", "list_dir", "web_fetch", "web_search", "run_shell"}

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_FENCE = re.compile(r"^\s*(`{3,})")


def _escape(text: str) -> str:
    """Escape legacy-markdown specials so a stray ``_`` or ``*`` cannot open
    an unbalanced formatting run Telegram's parser then rejects."""
    return (
        text.replace("_", r"\_")
        .replace("*", r"\*")
        .replace("`", r"\`")
        .replace("[", r"\[")
    )


def _strip_table(row: str) -> str:
    cells = [c.strip() for c in row.strip().strip("|").split("|")]
    return " · ".join(c for c in cells if c)


def sanitise(text: str) -> str:
    """Downgrade generic markdown to the subset Telegram's legacy parser takes:
    headings become bold, tables become fenced summaries, nested list markers
    are flattened to ``- ``; everything else passes through."""
    out: list[str] = []
    in_table = False
    for line in text.splitlines():
        if _TABLE_ROW.match(line):
            if _TABLE_SEP.match(line):
                continue  # separator row (|---|---|) carries no information
            if not in_table:
                out.append("```")
                in_table = True
            out.append(_strip_table(line))
            continue
        if in_table:
            out.append("```")
            in_table = False
        heading = _HEADING.match(line)
        if heading:
            out.append(f"*{_escape(heading.group(2).strip())}*")
            continue
        item = _LIST_ITEM.match(line)
        if item:  # flatten nesting: indentation is meaningless on a phone
            out.append(f"- {item.group(3)}")
            continue
        out.append(line)
    if in_table:
        out.append("```")
    return "\n".join(out)


def split(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Split at ``limit`` chars without leaving a fenced code block open: a
    chunk cut inside a fence is closed there and the fence reopened in the next.
    A single line longer than the limit is hard-cut (Telegram has no overflow)."""
    if len(text) <= limit:
        return [text] if text else []
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        while len(line) > limit:  # unbreakable line: cut at the limit
            lines.append(line[:limit])
            line = line[limit:]
        lines.append(line)
    chunks: list[str] = []
    current: list[str] = []
    length = 0
    open_fences = 0  # fence parity inside the current chunk
    for line in lines:
        if length + len(line) > limit and current:
            broken = open_fences % 2 == 1
            if broken:
                current.append("```\n")
            chunks.append("".join(current))
            current = []
            length = 0
            if broken:
                current = ["```\n"]
                length += 4
        if _FENCE.match(line):
            open_fences += 1
        current.append(line)
        length += len(line)
    if current:
        if open_fences % 2:
            current.append("```\n")
        chunks.append("".join(current))
    return chunks


def sendable(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Sanitise then split: model text in, Telegram-ready chunks out."""
    return split(sanitise(text), limit)


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
    """Renders imp's agent events to Telegram: a typing action, one lazily
    created status message edited on a debounce, and the final answer as its
    own message(s)."""

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
        self._pending = False
        self._dirty = False  # text changed since the last successful edit

    async def handle(self, event: AgentEvent) -> None:
        if event.type is EventType.THINKING:
            await self.bot.send_chat_action(self.chat_id, "typing")
        elif event.type is EventType.REASONING:
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
        """Edit the status message if the text changed and the debounce allows.
        The first flush creates the message right away; afterwards edits are
        debounced (unless forced). On persistent edit failure, sends a fresh
        status message once and stops editing for the rest of the turn."""
        if not self._dirty:
            return
        if self.status_message_id is None:  # first reportable event: create now
            text = self.buffer.render()
            if not text:
                return
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False
            self._last_flush = time.monotonic()
            return
        now = time.monotonic()
        if not force and now - self._last_flush < self.edit_interval:
            self._pending = True
            return
        text = self.buffer.render()
        if not text:
            return
        if self.status_message_id is None:
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False
            self._last_flush = time.monotonic()
            return
        ok = await self.bot.edit_message(self.chat_id, self.status_message_id, text)
        if ok:
            self._dirty = False
            self._pending = False
            self._last_flush = time.monotonic()
        elif force:
            # persistent edit failure: one fresh message, then stop editing
            self.status_message_id = await self.bot.send_message(self.chat_id, text)
            self._dirty = False

    async def end_turn(self, summary: str) -> None:
        """Collapse the status to one line, then send the final answer as a
        new message (sanitised, split at 4096)."""
        self.buffer.collapse(summary)
        self._dirty = True  # collapse changed the text even if edits were current
        await self.flush(force=True)

    async def answer(self, text: str) -> None:
        for chunk in sendable(text, MAX_MESSAGE_CHARS):
            await self.bot.send_message(self.chat_id, chunk)

    @staticmethod
    def _tail(text: str, limit: int) -> str:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else ""

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
