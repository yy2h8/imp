"""Entry points and the Telegram long-poll loop.

`python -m assistant` runs the bot; `python -m assistant whoami` prints the
token's bot identity and the sender id of every message that arrives (run it
while the bot is stopped — two getUpdates consumers fight over the stream).
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

from imp.events import EventType

from .adapters import TelegramUIAdapter
from .adapters.telegram import TelegramBot, TelegramError, send_text
from .app import RESET_NOTICE, build_assistant, ensure_home
from .bootstrap import (
    needs_tailoring,
    prune_scratch,
    read_state,
    run_bootstrap,
    tailor_manual,
    write_state,
)
from .config import AssistantConfig
from .scheduler import Scheduler


def turn_summary(tools: int, started: float, ok: bool = True) -> str:
    """The one-line status collapse at turn end ."""
    elapsed = int(datetime.now(UTC).timestamp() - started)
    return f"{'✓' if ok else '✗'} done · {tools} tools · {elapsed} s"


def command_token(text: str) -> str:
    return text.split()[0].lower() if text.split() else ""


class TurnRunner:
    """One serialized turn : exact command tokens, the pre-turn reset
    check, event rendering into a per-turn UI adapter, and the final answer
    as its own message. `bot` is injectable so tests can pass a fake."""

    def __init__(self, app, bot, edit_interval: float, status_max_chars: int) -> None:
        self.app = app
        self.bot = bot
        self.edit_interval = edit_interval
        self.status_max_chars = status_max_chars

    async def run(self, prompt: str) -> None:
        """Run one owner prompt through the agent. Returns normally even when
        the turn fails: the error path messages the owner and polling
        continues. Required delivery failures propagate to the supervisor."""
        command = await asyncio.to_thread(self._command_reply, prompt)
        if command is not None:
            await send_text(self.bot, self.app.chat_id, command)
            return
        await asyncio.to_thread(self.app.refresh_prompt)
        notice = await asyncio.to_thread(self._reset_if_needed)
        if notice is not None:
            await send_text(self.bot, self.app.chat_id, notice)
        await self._agent_turn(prompt)

    def _command_reply(self, prompt: str) -> str | None:
        """`/new` and `/status` are answered directly, never as agent turns
        (exact command tokens)."""
        lowered = command_token(prompt)
        if lowered == "/new":
            name = self.app.reset()
            return f"Started a fresh session. Previous transcript is saved. New transcript: `{name}`"
        if lowered == "/status":
            used, maximum = self.app.usage
            name = self.app.session.writer.path.name
            return (
                f"*status:* {used}/{maximum} tokens ({used / maximum:.0%}) · "
                f"transcript `{name}`"
            )
        return None

    def _reset_if_needed(self) -> str | None:
        """Pre-turn overflow check : reset before answering."""
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
            async with aclosing(self.app.agent.run_turn(prompt)) as events:
                async for event in events:
                    if event.type is EventType.TOOL_START:
                        tools += 1
                    if event.type is EventType.ERROR:
                        error = event.error_message
                    if event.type is EventType.MODEL_RESPONSE:
                        reply = (
                            event.quote or ""
                        )  # the last one before the loop ends is final
                    await ui.handle(event)
                    await ui.flush()
        except Exception as exc:
            error = str(exc)
        if error is not None:
            await send_text(self.bot, self.app.chat_id, f"*error:* {error}")
            await ui.end_turn("✗ failed")
            return
        await ui.end_turn(turn_summary(tools, started))
        if reply:
            await ui.answer(reply)


def valid_request(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if not isinstance(value, dict) or set(value) != {"attachment"}:
        return False
    message = value["attachment"]
    if not isinstance(message, dict) or not isinstance(message.get("caption", ""), str):
        return False
    kinds = [key for key in ("document", "voice", "photo") if key in message]
    if len(kinds) != 1:
        return False
    kind = kinds[0]
    items = message[kind] if kind == "photo" else [message[kind]]
    return (
        isinstance(items, list)
        and bool(items)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("file_id"), str)
            and bool(item["file_id"].strip())
            for item in items
        )
    )


def validate_queue(state: dict) -> None:
    if (
        type(state.get("offset", 0)) is not int
        or state.get("offset", 0) < 0
        or not isinstance(state.get("pending_requests", []), list)
        or any(
            not valid_request(prompt) for prompt in state.get("pending_requests", [])
        )
        or (
            state.get("active_request") is not None
            and not valid_request(state.get("active_request"))
        )
    ):
        raise ValueError(
            "Invalid request queue in state.json; repair it before restarting"
        )


class PollLoop:
    """Persist accepted prompts and their Telegram cursor before running work.

    One worker drains the FIFO independently of long polling. A running request
    stays marked on disk until its turn finishes; a restart reports that request
    as interrupted instead of repeating potentially destructive actions.
    """

    def __init__(self, app, bot) -> None:
        self.app = app
        self.bot = bot
        self._state_lock = asyncio.Lock()
        self.turn_task: asyncio.Task | None = None
        state = read_state(app.assistant.home)
        self.offset = state.get("offset", 0)
        self.pending = state.get("pending_requests", [])
        self.active = state.get("active_request")
        validate_queue(state)

    async def poll_forever(self) -> None:
        try:
            if self.active is not None:
                await send_text(
                    self.bot,
                    self.app.chat_id,
                    "A request was interrupted by the previous shutdown. "
                    "It may already have performed actions, so I will not rerun it. "
                    "Check the previous transcript before resubmitting it. "
                    "Waiting requests will continue in a fresh conversation.",
                )
                async with self._state_lock:
                    await self._save(active_request=None)
            await self._drain_queue()
            while True:
                try:
                    updates = await self.bot.get_updates(self.offset)
                except TelegramError:
                    await asyncio.sleep(5)
                    continue
                for update in updates:
                    await self._handle_update(update)
                await self._drain_queue()
        finally:
            if self.turn_task is not None:
                self.turn_task.cancel()
                await asyncio.gather(self.turn_task, return_exceptions=True)

    async def _handle_update(self, update: dict) -> None:
        update_id = update.get("update_id")
        if type(update_id) is not int or update_id < self.offset:
            return
        message = update.get("message") or {}
        user_id = (message.get("from") or {}).get("id")
        prompt = None
        answer_allowed = False
        chat = message.get("chat") or {}
        if (
            user_id == self.app.chat_id
            and chat.get("id") == self.app.chat_id
            and chat.get("type") == "private"
        ):
            text = (message.get("text") or "").strip()
            if text:
                prompt = text
                answer_allowed = command_token(text) not in {"/new", "/status"}
            elif any(key in message for key in ("document", "photo", "voice")):
                attachment = {
                    key: message[key]
                    for key in ("document", "voice", "photo", "caption", "message_id")
                    if key in message
                }
                prompt = {"attachment": attachment}
                if not valid_request(prompt):
                    await send_text(
                        self.bot,
                        self.app.chat_id,
                        "Invalid attachment; please resend it.",
                    )
                    prompt = None

        question = self.app.ask_router.question
        async with self._state_lock:
            answering = (
                prompt is not None
                and answer_allowed
                and self.turn_task is not None
                and not self.turn_task.done()
                and question is not None
            )
            pending = self.pending + (
                [prompt] if prompt is not None and not answering else []
            )
            # One atomic write: an acknowledged update is either queued or an
            # answer to the active request, which will not replay after a crash.
            await self._save(offset=update_id + 1, pending_requests=pending)
            if answering and not self.app.ask_router.deliver(prompt, question):
                await self._save(pending_requests=self.pending + [prompt])
        await self._drain_queue()

    async def _drain_queue(self) -> None:
        if self.turn_task is not None:
            if not self.turn_task.done():
                return
            self.turn_task.result()  # surface worker/storage failures to the supervisor
        if self.pending:
            self.turn_task = asyncio.create_task(self._work())

    async def _work(self) -> None:
        while True:
            async with self.app.execution_lock:
                async with self._state_lock:
                    if not self.pending:
                        return
                    prompt = self.pending[0]
                    await self._save(
                        pending_requests=self.pending[1:], active_request=prompt
                    )
                if isinstance(prompt, dict):
                    prompt = await self.app.uploads.handle(prompt["attachment"])
                if prompt is not None:
                    await self._run_turn(prompt)
                async with self._state_lock:
                    await self._save(active_request=None)

    async def _run_turn(self, text: str) -> None:
        await TurnRunner(
            self.app,
            self.bot,
            self.app.assistant.edit_interval,
            self.app.assistant.status_max_chars,
        ).run(text)

    async def _save(self, **updates) -> None:
        # Call under _state_lock. Join the write even on cancellation so another
        # writer cannot race a still-running filesystem thread.
        write = asyncio.create_task(
            asyncio.to_thread(write_state, self.app.assistant.home, updates)
        )
        try:
            state = await asyncio.shield(write)
        except asyncio.CancelledError:
            await write
            raise
        self.offset = state.get("offset", 0)
        self.pending = state.get("pending_requests", [])
        self.active = state.get("active_request")


async def run_bot() -> None:
    """The bot flow: bootstrap, then poll forever with the scheduler beside it."""
    assistant_config = AssistantConfig.from_env()
    chat_id = next(iter(assistant_config.allowed_user_ids))
    ensure_home(assistant_config.home)  # first run: the home does not exist yet
    validate_queue(read_state(assistant_config.home))
    await startup(assistant_config, chat_id, force=False)
    async with build_assistant(assistant_config, chat_id) as app:
        prune_scratch(app.assistant.home, app.assistant.scratch_ttl_days)
        scheduler = asyncio.create_task(Scheduler(app).run())
        try:
            await PollLoop(app, app.bot).poll_forever()
        finally:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)


async def startup(assistant_config: AssistantConfig, chat_id: int, force: bool):
    """Bootstrap before the loop : probe → manual → fingerprint,
    then exactly one tailoring turn on first run, mismatch, or force.
    Returns the bootstrap result so tests can assert on it."""
    was_tailored = not needs_tailoring(read_state(assistant_config.home))
    result = await asyncio.to_thread(
        run_bootstrap, assistant_config.home, _package_dir(), force=force
    )
    if not result.changed and not needs_tailoring(read_state(assistant_config.home)):
        return result
    async with build_assistant(assistant_config, chat_id) as app:
        prune_scratch(app.assistant.home, app.assistant.scratch_ttl_days)
        try:
            await tailor_manual(app, result.probe)
        except Exception as exc:
            print(f"Manual tailoring failed: {exc}", file=sys.stderr)
            await send_text(
                app.bot,
                chat_id,
                "Manual tailoring failed. Continuing with the existing manual; "
                "I will retry at the next startup.",
            )
            return result
        if result.changed and was_tailored:
            await send_text(
                app.bot,
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
