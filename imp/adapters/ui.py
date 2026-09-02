from __future__ import annotations

from urllib.parse import urlparse

from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.shortcuts import PromptSession
from rich.console import Console
from rich.markdown import Markdown
from rich.status import Status
from rich.syntax import Syntax
from rich.text import Text

from ..config import Config
from ..events import AgentEvent, EventType

QUIET_TOOLS = {"read_file", "ask", "list_dir", "web_fetch", "web_search", "run_shell"}

YELLOW = "\x1b[33m"
RESET = "\x1b[0m"


def _tail(text: str, limit: int) -> str:
    """Keep the last `limit` lines; a leading "..." marks a crop."""
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    return "...\n" + "\n".join(lines[-limit:])


def _multiline_bindings() -> KeyBindings:
    """Enter accepts, Esc+Enter inserts a newline; pasted text stays verbatim
    (bracketed paste never routes through key bindings)."""
    kb = KeyBindings()

    @kb.add("enter")
    def _accept(event: KeyPressEvent) -> None:
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    return kb


class UIAdapter:
    """Owns all terminal interaction: prompt_toolkit for input, rich for output."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.console = Console()
        self.session: PromptSession[str] = PromptSession(
            multiline=True,
            key_bindings=_multiline_bindings(),
            erase_when_done=True,
        )
        # lazy: approvals/questions get their own session so they never
        # pollute the main input history
        self.ask_session: PromptSession[str] = PromptSession()
        self._status: Status | None = None
        self._fresh = True  # at a section boundary; nothing printed yet

    def banner(self) -> None:
        host = urlparse(self.config.base_url).hostname or self.config.base_url
        hints = "Ctrl-D exit · end a line with \\ to continue"
        lines = [
            "imp",
            f"model     {self.config.model} @ {host}",
            f"workspace {self.config.workspace}",
            hints,
        ]
        for line in lines:
            self.console.print(Text("│ ", style="cyan"), Text(line), sep="")
        self._fresh = False

    async def prompt(self) -> str:
        """Read user input; a trailing backslash continues onto the next line."""
        self._separator()  # before prompt_async: prompt_toolkit's erase must not swallow it
        text = await self.session.prompt_async(ANSI(f"{YELLOW}>{RESET} "))
        while text.rstrip().endswith("\\"):
            text = text.rstrip()[:-1] + "\n"
            text += await self.session.prompt_async(ANSI(f"{YELLOW}... {RESET}"))
        for line in text.splitlines():
            self.console.print(Text(f"│ {line}", style="yellow"))
            self._fresh = False
        return text

    async def ask(self, message: str, markdown: bool = True) -> str:
        """Read an answer to a question/approval message."""
        self._separator()
        self.console.print(Markdown(message) if markdown else Text(message))
        self._fresh = False
        return await self.ask_session.prompt_async(ANSI(f"\n{YELLOW}>>{RESET} "))

    def error(self, message: str) -> None:
        self.console.print(Text(message), style="red")
        self._fresh = False

    def _start_spinner(self) -> None:
        if self._status is None:
            self._separator()
            self._status = self.console.status("thinking…")
            self._status.start()

    def stop_spinner(self) -> None:
        if self._status is not None:
            self._status.stop()
            self._status = None

    def _truncate(self, text: str) -> str:
        lines = text.splitlines()
        limit = self.config.max_tool_display_lines
        if len(lines) <= limit:
            return text.rstrip("\n")
        return "\n".join(lines[:limit]) + f"\n… [+{len(lines) - limit} lines truncated]"

    @staticmethod
    def _format_args(args: dict | None) -> str:
        if not args:
            return ""
        parts = []
        for key, value in args.items():
            rendered = str(value).replace("\n", " ")
            if len(rendered) > 80:
                rendered = rendered[:80] + "…"
            parts.append(f"{key}={rendered}")
        return " ".join(parts)

    def _separator(self) -> None:
        """One blank line before a new section; collapses if already at one."""
        if not self._fresh:
            self.console.print()
            self._fresh = True

    def end_turn(self, usage: tuple[int, int]) -> None:
        self._separator()
        used, maximum = usage
        filled = round(min(used / maximum, 1.0) * 10)
        bar = "█" * filled + "░" * (10 - filled)
        self.console.print(
            f"{used:,}/{maximum:,} tokens ({used / maximum:.0%}) {bar}",
            style="bright_black",
            justify="right",
        )
        self._fresh = False

    def render_event(self, event: AgentEvent) -> None:
        if event.type is EventType.THINKING:
            self._start_spinner()
        elif event.type is EventType.REASONING:
            self.stop_spinner()
            if event.quote:
                self._separator()
                cropped = _tail(event.quote, self.config.max_reasoning_display_lines)
                self.console.print(Text(cropped), style="italic bright_black")
                self._fresh = False
        elif event.type is EventType.MODEL_RESPONSE:
            self.stop_spinner()
            if event.quote:
                self._separator()
                self.console.print(Markdown(event.quote))
                self._fresh = False
                self._separator()
            return  # no usage line; end_turn reports it once per turn
        elif event.type is EventType.TOOL_START:
            self.stop_spinner()
            self._separator()
            args = Text(f" {self._format_args(event.tool_args)}")
            self.console.print(Text(f"[{event.tool_name}]", style="cyan"), args)
            self._fresh = False
        elif event.type is EventType.TOOL_RESULT and event.tool_result:
            result = event.tool_result
            quiet = event.tool_name in QUIET_TOOLS
            mark = "ok" if result.ok else "failed"
            style = "green" if result.ok else "red"
            if quiet and result.ok:
                return  # reads/questions: result content is noise in the UI
            self.console.print(Text(f"← {mark}", style=style))
            self._fresh = False
            content = self._truncate(result.content)
            if content:
                self.console.print(Text(content), style="bright_black")
            if result.diff:
                self.console.print(Syntax(self._truncate(result.diff), "diff"))
        elif event.type is EventType.ERROR and event.error_message:
            self.stop_spinner()
            self._separator()
            self.error(f"error: {event.error_message}")
