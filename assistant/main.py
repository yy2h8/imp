"""aiogram entry points, private-owner message handler and turn worker."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from contextlib import aclosing, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from aiogram import Dispatcher
from aiogram.types import Message

from imp.events import EventType

from .adapters.telegram import TelegramBot, TelegramError, send_text
from .adapters.ui import TelegramUIAdapter, turn_summary
from .app import RESET_NOTICE, AssistantApp, build_assistant, ensure_home
from .bootstrap import (
    needs_tailoring,
    prune_scratch,
    read_state,
    run_bootstrap,
    tailor_manual,
)
from .config import AssistantConfig
from .db import (
    queue_claim_next,
    queue_finish,
    queue_finish_album,
    queue_interrupted,
    queue_resume_collecting,
    turn_insert,
)
from .intake import AlbumBuffer, Intake, route_message
from .scheduler import set_context, startup_recovery
from .status import collect_status

_LOG = logging.getLogger(__name__)
TYPING_INTERVAL_S = 4.0


def _preview(text: str, limit: int = 60) -> str:
    line = " ".join(text.split())
    return line[:limit] + ("…" if len(line) > limit else "")


def command_token(text: str) -> str:
    return text.split()[0].lower() if text.split() else ""


class TurnRunner:
    """Run one queued owner request; own the interactive live status and log."""

    def __init__(self, app: AssistantApp, bot, edit_interval: float, status_max_chars: int) -> None:
        self.app = app
        self.bot = bot
        self.edit_interval = edit_interval
        self.status_max_chars = status_max_chars

    @asynccontextmanager
    async def _turn_scope(self):
        previous = self.app.turn_state["active"]
        self.app.turn_state["active"] = True
        try:
            if self.app.outbox is None:
                yield
            else:
                async with self.app.outbox.turn_scope():
                    yield
        finally:
            self.app.turn_state["active"] = previous

    async def run(self, prompt: str) -> None:
        async with self._turn_scope():
            command = await self._command(prompt)
            if command is not None:
                await send_text(self.bot, self.app.chat_id, command)
                return
            _LOG.info("turn start: %s", _preview(prompt))
            await asyncio.to_thread(self.app.refresh_prompt)
            notice = await asyncio.to_thread(self._reset_if_needed)
            if notice is not None:
                await send_text(self.bot, self.app.chat_id, notice)
            await self._agent_turn(prompt)

    async def _command(self, prompt: str) -> str | None:
        if command_token(prompt) == "/status":
            return await collect_status(self.app)
        return await asyncio.to_thread(self._command_reply, prompt)

    def _command_reply(self, prompt: str) -> str | None:
        lowered = command_token(prompt)
        if lowered == "/new":
            name = self.app.reset()
            _LOG.info("command /new: fresh transcript %s", name)
            return (
                "Started a fresh session. Previous transcript is saved. "
                f"New transcript: `{name}`"
            )
        return None

    def _reset_if_needed(self) -> str | None:
        if self.app.should_reset():
            name = self.app.reset()
            return f"{RESET_NOTICE} New transcript: `{name}`"
        return None

    async def _agent_turn(self, prompt: str) -> None:
        started = time.monotonic()
        started_at = datetime.now(UTC).isoformat()
        ui = TelegramUIAdapter(
            self.bot,
            self.app.chat_id,
            edit_interval=self.edit_interval,
            max_chars=self.status_max_chars,
        )
        await ui.begin()
        typing = asyncio.create_task(self._keep_typing())
        tools = 0
        error: str | None = None
        reply = ""
        in_tokens = out_tokens = 0
        reported_cost = 0.0
        saw_model_response = False
        cost_complete = True
        try:
            async with aclosing(self.app.agent.run_turn(prompt)) as events:
                async for event in events:
                    if event.type is EventType.TOOL_START:
                        tools += 1
                        _LOG.info("tool: %s", event.tool_name)
                    elif event.type is EventType.ERROR:
                        error = event.error_message or "Turn failed"
                    elif event.type is EventType.MODEL_RESPONSE:
                        reply = event.quote or ""
                        saw_model_response = True
                        if event.usage is None:
                            cost_complete = False
                        else:
                            in_tokens += event.usage.input_tokens
                            out_tokens += event.usage.output_tokens
                            if event.usage.cost_usd is None:
                                cost_complete = False
                            else:
                                reported_cost += event.usage.cost_usd
                    await ui.handle(event)
                    await ui.flush()
        except Exception as exc:
            error = str(exc)
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)

        seconds = int(time.monotonic() - started)
        ok = error is None
        cost = reported_cost if saw_model_response and cost_complete else None
        if self.app.db is not None:
            await turn_insert(
                self.app.db,
                ts=started_at,
                kind="interactive",
                session_id=self.app.session.writer.name,
                model=self.app.config.model,
                in_tokens=in_tokens,
                out_tokens=out_tokens,
                cost_usd=cost,
                tools=tools,
                seconds=seconds,
                ok=ok,
            )
        _LOG.info("turn finished: ok=%s tools=%d elapsed=%ds", ok, tools, seconds)
        if error is not None:
            _LOG.warning("turn failed: %s", error)
            await send_text(self.bot, self.app.chat_id, f"*error:* {error}")
            await ui.end_turn(turn_summary(tools, seconds, False, None))
            return
        await ui.end_turn(turn_summary(tools, seconds, True, cost))
        if reply:
            await ui.answer(reply)

    async def _keep_typing(self) -> None:
        while True:
            await self.bot.send_chat_action(self.app.chat_id, "typing")
            await asyncio.sleep(TYPING_INTERVAL_S)


class AssistantController:
    """Translate private-owner updates to the DB FIFO and drain one turn at a time."""

    def __init__(self, app: AssistantApp) -> None:
        if app.db is None:
            raise ValueError("AssistantController requires the state database")
        self.app = app
        self.intake = Intake(
            app.db,
            app.bot,
            app.chat_id,
            is_busy=lambda: app.turn_state["active"],
        )
        self.albums = AlbumBuffer()
        self.turn_task: asyncio.Task | None = None
        self.album_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._working = False

    async def start(self) -> None:
        await queue_resume_collecting(self.app.db)
        self.album_task = asyncio.create_task(self._flush_albums())
        self.turn_task = asyncio.create_task(self._work())
        self._wake.set()

    async def close(self) -> None:
        if self.album_task is not None:
            self.album_task.cancel()
            await asyncio.gather(self.album_task, return_exceptions=True)
        # A graceful shutdown persists groups still inside their debounce window.
        for album_id in await self.albums.flush_all():
            await queue_finish_album(self.app.db, album_id)
        if self.turn_task is not None and not self.turn_task.done():
            self.turn_task.cancel()
            await asyncio.gather(self.turn_task, return_exceptions=True)

    async def handle_message(self, message: Message | dict) -> None:
        if not isinstance(message, dict):
            message = message.model_dump(exclude_none=True, by_alias=True)
        routed = route_message(
            message,
            owner_id=self.app.chat_id,
            question_pending=self.app.ask_router.pending,
            busy=self.app.turn_state["active"],
        )
        if routed.ignore:
            return
        message_id = message.get("message_id")
        if routed.answer is not None:
            if await self.intake.mark_seen(message_id) and not self.app.ask_router.deliver(routed.answer):
                # The question ended during routing; preserve the message as work.
                message["text"] = routed.answer
                ack = await self.intake.accept_unseen(message)
                if ack:
                    await send_text(self.app.bot, self.app.chat_id, ack)
                await self._drain_queue()
            return
        if routed.attachment and message.get("media_group_id"):
            ack = await self.intake.accept_album_item(message)
            if ack:
                await send_text(self.app.bot, self.app.chat_id, ack)
            await self.albums.add(message)
            await self._drain_queue()
            return
        if routed.text is not None:
            message["text"] = routed.text
        ack = await self.intake.accept(message)
        if ack:
            await send_text(self.app.bot, self.app.chat_id, ack)
        await self._drain_queue()

    async def wait_idle(self) -> None:
        while self._working:
            await asyncio.sleep(0.01)
        while True:
            rows = await self.app.db.execute_fetchall("SELECT COUNT(*) FROM queue")
            if rows[0][0] == 0 and not self._working:
                return
            await asyncio.sleep(0.01)

    async def _flush_albums(self) -> None:
        while True:
            await asyncio.sleep(0.1)
            for album_id in await self.albums.flush_due():
                await queue_finish_album(self.app.db, album_id)
                await self._drain_queue()

    async def _drain_queue(self) -> None:
        if self.turn_task is None or self.turn_task.done():
            self.turn_task = asyncio.create_task(self._work())
        self._wake.set()

    @asynccontextmanager
    async def _busy_scope(self):
        previous = self.app.turn_state["active"]
        self.app.turn_state["active"] = True
        try:
            if self.app.outbox is None:
                yield
            else:
                async with self.app.outbox.turn_scope():
                    yield
        finally:
            self.app.turn_state["active"] = previous

    async def _work(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            while True:
                row = await queue_claim_next(self.app.db)
                if row is None:
                    break
                row_id, kind, raw = row
                completed = False
                self._working = True
                try:
                    payload = json.loads(raw)
                    async with self._busy_scope():
                        if kind == "attachment":
                            prompt = await self.app.uploads.handle(
                                payload.get("attachments", []),
                                payload.get("caption", ""),
                                payload.get("reply_context", ""),
                            )
                        else:
                            prompt = payload.get("text", "")
                        if prompt:
                            await TurnRunner(
                                self.app,
                                self.app.bot,
                                self.app.assistant.edit_interval,
                                self.app.assistant.status_max_chars,
                            ).run(prompt)
                    completed = True
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _LOG.exception("queued request %s failed", row_id)
                    await send_text(
                        self.app.bot, self.app.chat_id, "*error:* queued request failed"
                    )
                    completed = True
                finally:
                    if completed:
                        await queue_finish(self.app.db, row_id)
                    self._working = False


def build_dispatcher(controller: AssistantController) -> Dispatcher:
    dispatcher = Dispatcher()

    @dispatcher.message()
    async def owner_message(message: Message) -> None:
        await controller.handle_message(message)

    return dispatcher


async def _recover_interactive_requests(app: AssistantApp) -> None:
    if app.db is None:
        return
    for row_id, _kind, _payload in await queue_interrupted(app.db):
        await send_text(
            app.bot,
            app.chat_id,
            "A request was interrupted by the previous shutdown. It may already "
            "have performed actions, so I will not rerun it. Check the previous "
            f"transcript before resubmitting it (queue item {row_id}).",
        )


async def run_bot() -> None:
    assistant_config = AssistantConfig.from_env()
    logging.getLogger().setLevel(assistant_config.log_level.upper())
    chat_id = next(iter(assistant_config.allowed_user_ids))
    ensure_home(assistant_config.home)
    await startup(assistant_config, chat_id, force=False)
    async with build_assistant(assistant_config, chat_id) as app:
        _LOG.info(
            "assistant ready: model=%s home=%s owner=%s",
            app.config.model,
            app.assistant.home,
            chat_id,
        )
        prune_scratch(app.assistant.home, app.assistant.scratch_ttl_days)
        await _recover_interactive_requests(app)
        if app.job_context is not None:
            await startup_recovery(app.job_context)
        controller = AssistantController(app)
        dispatcher = build_dispatcher(controller)
        app.scheduler.start()
        await app.outbox.start()
        await controller.start()
        try:
            try:
                await send_text(app.bot, chat_id, await collect_status(app))
            except Exception as exc:
                _LOG.warning("startup notification failed: %s", exc)
            await dispatcher.start_polling(
                app.bot.client,
                allowed_updates=dispatcher.resolve_used_update_types(),
                handle_as_tasks=False,
                close_bot_session=False,
            )
        finally:
            await controller.close()
            app.scheduler.shutdown(wait=False)
            await app.outbox.stop()
            set_context(None)


async def startup(assistant_config: AssistantConfig, chat_id: int, force: bool):
    """Bootstrap: probe → manual → fingerprint, then optional tailoring turn."""
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
    """Identify the owner using aiogram's Bot API client; run while stopped."""
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
                        for part in (sender.get("first_name"), sender.get("last_name"))
                        if part
                    )
                    print(f"id: {sender['id']}  name: {name}")
    finally:
        await bot.close()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
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
