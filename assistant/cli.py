"""Entry point: argparse, the Telegram long-poll loop, and the serialized
turn runner (one turn at a time; mid-turn messages are routed by AskRouter)."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from pathlib import Path

from imp.events import EventType

from .adapters import TelegramUIAdapter
from .adapters.telegram import TelegramError
from .app import RESET_NOTICE, build_assistant, ensure_home
from .bootstrap import (
    Probe,
    prune_scratch,
    read_state,
    run_bootstrap,
    tailor_manual,
    write_state,
)
from .config import AssistantConfig
from .scheduler import Scheduler

PACKAGE_DIR = Path(__file__).resolve().parent


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assistant",
        description="A personal assistant reached over Telegram, built on imp.",
        epilog="Configuration comes from environment variables only; "
        "see assistant/.env.example.",
    )
    p.add_argument(
        "--rebootstrap",
        action="store_true",
        help="Rewrite AGENTS.md from a fresh probe, then re-run the tailoring turn.",
    )
    p.add_argument(
        "--probe",
        action="store_true",
        help="Print the environment probe and exit without writing anything.",
    )
    return p


def turn_summary(tools: int, started: float, ok: bool = True) -> str:
    """The one-line status collapse at turn end (spec §4.3)."""
    elapsed = int(datetime.now(UTC).timestamp() - started)
    return f"{'✓' if ok else '✗'} done · {tools} tools · {elapsed} s"


class TurnRunner:
    """One serialized turn (spec §4.1): command prefixes, the pre-turn reset
    check, event rendering into a per-turn UI adapter, and the final answer
    as its own message. `bot` is injectable so tests can pass a fake."""

    def __init__(
        self, app, bot, edit_interval: float, status_max_chars: int
    ) -> None:
        self.app = app
        self.bot = bot
        self.edit_interval = edit_interval
        self.status_max_chars = status_max_chars

    async def run(self, prompt: str) -> None:
        """Run one owner prompt through the agent. Returns normally even when
        the turn fails: the error path messages the owner and polling
        continues (spec §4.5)."""
        command = self._command_reply(prompt)
        if command is not None:
            await self.bot.send_message(self.app.chat_id, command)
            return
        notice = self._reset_if_needed()
        if notice is not None:
            await self.bot.send_message(self.app.chat_id, notice)
        await self._agent_turn(prompt)

    def _command_reply(self, prompt: str) -> str | None:
        """`/new` and `/status` are answered directly, never as agent turns
        (spec §5: prefix-matched, no command framework)."""
        lowered = prompt.lower()
        if lowered.startswith("/new"):
            name = self.app.reset()
            return f"{RESET_NOTICE} New transcript: `{name}`"
        if lowered.startswith("/status"):
            used, maximum = self.app.usage
            name = self.app.session.writer.path.name
            return (
                f"*status:* {used}/{maximum} tokens ({used / maximum:.0%}) · "
                f"transcript `{name}`"
            )
        return None

    def _reset_if_needed(self) -> str | None:
        """Pre-turn overflow check (spec §5): reset before answering."""
        if self.app.should_reset():
            name = self.app.reset()
            return f"{RESET_NOTICE} New transcript: `{name}`"
        return None

    async def _agent_turn(self, prompt: str) -> None:
        started = datetime.now(UTC).timestamp()
        ui = TelegramUIAdapter(
            self.bot,
            self.app.chat_id,
            edit_interval=self.edit_interval,
            max_chars=self.status_max_chars,
        )
        tools = 0
        error: str | None = None
        reply = ""
        try:
            async for event in self.app.agent.run_turn(prompt):
                if event.type is EventType.TOOL_START:
                    tools += 1
                if event.type is EventType.ERROR:
                    error = event.error_message
                if event.type is EventType.MODEL_RESPONSE and event.quote:
                    reply = event.quote  # the last one before the loop ends is final
                await ui.handle(event)
                await ui.flush()
        except Exception as exc:
            error = str(exc)
        if error is not None:
            await self.bot.send_message(self.app.chat_id, f"*error:* {error}")
            await ui.end_turn("✗ failed")
            return
        await ui.end_turn(turn_summary(tools, started))
        if reply:
            await ui.answer(reply)


class PollLoop:
    """The long-poll loop (spec §4.1): getUpdates → offset to state.json →
    owner text into the serialized turn runner. Turns run as tasks so
    polling never stops: a message that arrives mid-turn answers a pending
    ask, is held for the next one, or becomes the next prompt."""

    def __init__(self, app, bot) -> None:
        self.app = app
        self.bot = bot
        self.turn_lock = asyncio.Lock()
        self.turn_task: asyncio.Task | None = None
        self.offset = int(read_state(app.assistant.home).get("offset", 0))

    async def poll_forever(self) -> None:
        while True:
            try:
                updates = await self.bot.get_updates(self.offset)
            except TelegramError:
                await asyncio.sleep(5)  # §4.5: back off, session untouched
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    self.offset = update_id + 1
            if updates:
                self._save_offset()
            for update in updates:
                await self._handle_update(update)
            await self._drain_queue()

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message") or {}
        user_id = (message.get("from") or {}).get("id")
        if user_id not in self.app.assistant.allowed_user_ids:
            return
        text = (message.get("text") or "").strip()
        if not text:
            return
        if self.turn_task is not None and not self.turn_task.done():
            self.app.ask_router.deliver(text)  # an ask's answer, or held for next
            return
        self.turn_task = asyncio.create_task(self._run_turn(text))

    async def _drain_queue(self) -> None:
        """Promote a message that raced a pending ask into the next turn."""
        if (
            self.turn_task is None or self.turn_task.done()
        ) and (held := self.app.ask_router.take()) is not None:
            self.turn_task = asyncio.create_task(self._run_turn(held))

    async def _run_turn(self, text: str) -> None:
        async with self.turn_lock:  # serialized turns, reset only between them
            await TurnRunner(
                self.app,
                self.bot,
                self.app.assistant.edit_interval,
                self.app.assistant.status_max_chars,
            ).run(text)

    def _save_offset(self) -> None:
        write_state(self.app.assistant.home, {"offset": self.offset})


async def startup(assistant_config: AssistantConfig, chat_id: int, force: bool):
    """Bootstrap before the loop (spec §6): probe → manual → fingerprint,
    then exactly one tailoring turn on first run, mismatch, or --rebootstrap.
    Returns the bootstrap result so tests can assert on it."""
    result = run_bootstrap(
        assistant_config.home, PACKAGE_DIR, force=force
    )
    if not result.changed:
        return result
    first = "tailored" not in read_state(assistant_config.home) or force
    async with build_assistant(assistant_config, chat_id) as app:
        prune_scratch(app.assistant.home, app.assistant.scratch_ttl_days)
        await tailor_manual(app, result.probe)
        if not first:
            await app.bot.send_message(
                chat_id,
                "The machine's environment changed — `AGENTS.md` was "
                "regenerated from a fresh probe and re-tailored.",
            )
    return result


async def amain(args: argparse.Namespace) -> None:
    assistant_config = AssistantConfig.from_env()
    chat_id = min(assistant_config.allowed_user_ids)
    if args.probe:
        print(Probe.take(assistant_config.home).render())
        return
    ensure_home(assistant_config.home)  # first run: the home does not exist yet
    await startup(assistant_config, chat_id, force=args.rebootstrap)
    async with build_assistant(assistant_config, chat_id) as app:
        prune_scratch(app.assistant.home, app.assistant.scratch_ttl_days)
        scheduler = asyncio.create_task(Scheduler(app).run())
        try:
            await PollLoop(app, app.bot).poll_forever()
        finally:
            scheduler.cancel()


def main() -> None:
    args = parser().parse_args()
    try:
        asyncio.run(amain(args))
    except KeyboardInterrupt:
        pass
    except ValueError as e:
        raise SystemExit(f"Configuration error: {e}")


if __name__ == "__main__":
    main()
