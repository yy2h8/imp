"""Entry points and the Telegram long-poll loop.

`python -m assistant` runs the bot; `python -m assistant whoami` prints the
token's bot identity and the sender id of every message that arrives (run it
while the bot is stopped — two getUpdates consumers fight over the stream).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

from imp.events import EventType

from .adapters import TelegramUIAdapter
from .adapters.telegram import TelegramBot, TelegramError
from .app import RESET_NOTICE, build_assistant, ensure_home
from .bootstrap import (
    prune_scratch,
    read_state,
    run_bootstrap,
    tailor_manual,
    write_state,
)
from .config import AssistantConfig
from .scheduler import Scheduler


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
    owner text or upload into the serialized turn runner. Turns run as tasks
    so polling never stops: a message that arrives mid-turn answers a pending
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
            upload_prompt = await self.app.uploads.handle(message)
            if upload_prompt is not None:
                self._submit_upload(upload_prompt)
            return
        await self._submit(text)

    def _submit_upload(self, prompt: str) -> None:
        """A captioned upload starts the next turn — but never resolves a
        pending ask (spec §4: uploads are not answers)."""
        if self.turn_task is not None and not self.turn_task.done():
            self.app.ask_router.hold(prompt)
            return
        self.turn_task = asyncio.create_task(self._run_turn(prompt))

    async def _submit(self, prompt: str) -> None:
        """Start a turn, or route the prompt to a pending ask / the held slot
        when a turn is already running."""
        if self.turn_task is not None and not self.turn_task.done():
            self.app.ask_router.deliver(prompt)  # an ask's answer, or held
            return
        self.turn_task = asyncio.create_task(self._run_turn(prompt))

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


async def run_bot() -> None:
    """The bot flow: bootstrap, then poll forever with the scheduler beside it."""
    assistant_config = AssistantConfig.from_env()
    chat_id = min(assistant_config.allowed_user_ids)
    ensure_home(assistant_config.home)  # first run: the home does not exist yet
    await startup(assistant_config, chat_id, force=False)
    async with build_assistant(assistant_config, chat_id) as app:
        prune_scratch(app.assistant.home, app.assistant.scratch_ttl_days)
        scheduler = asyncio.create_task(Scheduler(app).run())
        try:
            await PollLoop(app, app.bot).poll_forever()
        finally:
            scheduler.cancel()


async def startup(assistant_config: AssistantConfig, chat_id: int, force: bool):
    """Bootstrap before the loop (spec §6): probe → manual → fingerprint,
    then exactly one tailoring turn on first run, mismatch, or force.
    Returns the bootstrap result so tests can assert on it."""
    result = run_bootstrap(assistant_config.home, _package_dir(), force=force)
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


def _package_dir() -> Path:
    return Path(__file__).resolve().parent


async def whoami() -> None:
    """Identify the owner: token check via getMe, then print the sender id of
    every incoming message until interrupted. Uses only the bot token."""
    token = AssistantConfig.raw_token()
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and retry."
        )
    bot = TelegramBot(token)
    try:
        me = await bot.get_me()
        who = " ".join(
            part for part in (me.get("first_name"), me.get("last_name")) if part
        )
        print(f"bot: @{me.get('username')} ({who or 'unnamed'})")
        print("send your bot a message; Ctrl-C to stop")
        offset = 0
        while True:
            try:
                updates = await bot.get_updates(offset)
            except TelegramError as exc:
                print(f"getUpdates failed: {exc}", file=sys.stderr)
                await asyncio.sleep(5)
                continue
            for update in updates:
                uid = update.get("update_id")
                if isinstance(uid, int):
                    offset = uid + 1
                message = update.get("message") or {}
                sender = message.get("from") or {}
                if sender.get("id") is not None:
                    name = " ".join(
                        part
                        for part in (
                            sender.get("first_name"),
                            sender.get("last_name"),
                        )
                        if part
                    )
                    print(f"id: {sender['id']}  name: {name}")
    finally:
        await bot.close()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else ""
    if command == "whoami":
        try:
            asyncio.run(whoami())
        except KeyboardInterrupt:
            pass
        except ValueError as e:
            raise SystemExit(f"Configuration error: {e}")
        return
    if command:
        raise SystemExit(f"unknown command: {command!r} (use 'whoami' or none)")
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
    except ValueError as e:
        raise SystemExit(f"Configuration error: {e}")


if __name__ == "__main__":
    main()
