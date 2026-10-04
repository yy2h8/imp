from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import assistant.main as entry
from assistant.adapters.telegram import TelegramError
from assistant.main import run_bot


class Scheduler:
    def __init__(self, events):
        self.events = events

    def start(self):
        self.events.append("scheduler-start")

    def shutdown(self, wait):
        self.events.append("scheduler-stop")


class Outbox:
    def __init__(self, events):
        self.events = events

    async def start(self):
        self.events.append("outbox-start")

    async def stop(self):
        self.events.append("outbox-stop")


class Controller:
    def __init__(self, events):
        self.events = events

    async def start(self):
        self.events.append("controller-start")

    async def close(self):
        self.events.append("controller-stop")


def install_bot(monkeypatch, tmp_path, send_error=None):
    events = []
    config = SimpleNamespace(
        allowed_user_ids={42}, home=tmp_path, log_level="INFO"
    )
    app = SimpleNamespace(
        config=SimpleNamespace(model="test-model"),
        assistant=SimpleNamespace(home=tmp_path, scratch_ttl_days=7),
        job_context=None,
        db=None,
        scheduler=Scheduler(events),
        outbox=Outbox(events),
        bot=SimpleNamespace(client=object()),
        chat_id=42,
    )

    @asynccontextmanager
    async def build(_config, _chat_id):
        yield app

    async def no_startup(*_args, **_kwargs):
        return None

    async def polling(*_args):
        events.append("polling-start")

    async def send(_bot, chat_id, text):
        events.append(("send", chat_id, text))
        if send_error is not None:
            raise send_error

    monkeypatch.setattr("assistant.main.AssistantConfig.from_env", lambda: config)
    monkeypatch.setattr("assistant.main.ensure_home", lambda _home: None)
    monkeypatch.setattr("assistant.main.startup", no_startup)
    monkeypatch.setattr("assistant.main.build_assistant", build)
    monkeypatch.setattr("assistant.main.prune_scratch", lambda *_args: None)
    monkeypatch.setattr("assistant.main._recover_interactive_requests", no_startup)
    monkeypatch.setattr("assistant.main.AssistantController", lambda _app: Controller(events))
    monkeypatch.setattr("assistant.main.poll_updates", polling)
    monkeypatch.setattr("assistant.main.send_text", send)
    monkeypatch.setattr("assistant.main.set_context", lambda _context: None)
    return events


async def test_run_bot_sends_status_summary_after_startup_and_before_polling(
    monkeypatch, tmp_path
):
    events = install_bot(monkeypatch, tmp_path)

    async def summary(_app):
        return "*Статус* сводка"

    monkeypatch.setattr("assistant.main.collect_status", summary)
    await run_bot()

    sent = ("send", 42, "*Статус* сводка")
    assert events.index("controller-start") < events.index(sent)
    assert events.index(sent) < events.index("polling-start")


async def test_run_bot_continues_polling_if_startup_status_fails(
    monkeypatch, tmp_path, caplog
):
    events = install_bot(monkeypatch, tmp_path, TelegramError("unavailable"))

    async def summary(_app):
        return "*Статус* сводка"

    monkeypatch.setattr("assistant.main.collect_status", summary)
    await run_bot()

    assert "polling-start" in events
    assert "startup notification failed" in caplog.text


async def test_polling_acknowledges_only_after_intake_and_respects_retry_after(monkeypatch):
    offsets, accepted, delays = [], [], []
    pages = [
        TelegramError("rate limited", retry_after=7),
        [{"update_id": 10, "message": {"message_id": 1}}],
        [{"update_id": 11, "message": {"message_id": 2}}],
    ]

    async def updates(offset):
        offsets.append(offset)
        if offset == 11:
            assert accepted == [1]
        if not pages:
            raise asyncio.CancelledError
        page = pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page

    async def accept(message):
        accepted.append(message["message_id"])

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(entry.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await entry.poll_updates(
            SimpleNamespace(get_updates=updates), SimpleNamespace(handle_message=accept)
        )
    assert offsets == [0, 0, 11, 12]
    assert accepted == [1, 2]
    assert delays == [7]


async def test_polling_retries_failed_intake_without_acknowledging(monkeypatch):
    offsets, accepted = [], []

    async def updates(offset):
        offsets.append(offset)
        if len(offsets) == 3:
            raise asyncio.CancelledError
        return [{"update_id": 10, "message": {"message_id": 1}}]

    async def accept(message):
        if len(offsets) == 1:
            raise OSError("database unavailable")
        accepted.append(message["message_id"])

    async def sleep(_delay):
        pass

    monkeypatch.setattr(entry.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await entry.poll_updates(
            SimpleNamespace(get_updates=updates), SimpleNamespace(handle_message=accept)
        )
    assert offsets == [0, 0, 11]
    assert accepted == [1]


def test_debug_logging_does_not_expose_bot_token(monkeypatch, caplog):
    import logging

    from telegram import User

    from assistant.adapters.telegram import TelegramBot

    token = "123456:secret-test-token"

    async def get_me(_bot):
        return User(id=1, is_bot=True, first_name="Test")

    async def run():
        logging.getLogger().setLevel(logging.DEBUG)
        bot = TelegramBot(token)
        try:
            await bot.initialize()
        finally:
            await bot.close()

    monkeypatch.setattr("telegram.Bot.get_me", get_me)
    monkeypatch.setattr(entry, "run_bot", run)
    with caplog.at_level(logging.DEBUG):
        entry.main([])
    assert token not in caplog.text


async def test_run_bot_continues_polling_if_collect_status_raises(
    monkeypatch, tmp_path, caplog
):
    events = install_bot(monkeypatch, tmp_path)

    async def broken(_app):
        raise RuntimeError("collector broken")

    monkeypatch.setattr("assistant.main.collect_status", broken)
    await run_bot()

    assert "polling-start" in events
    assert "startup notification failed" in caplog.text
