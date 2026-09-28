from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

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


class Dispatcher:
    def __init__(self, events):
        self.events = events

    def resolve_used_update_types(self):
        return []

    async def start_polling(self, client, **kwargs):
        self.events.append("polling-start")


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
    monkeypatch.setattr("assistant.main.build_dispatcher", lambda _controller: Dispatcher(events))
    monkeypatch.setattr("assistant.main.send_text", send)
    monkeypatch.setattr("assistant.main.set_context", lambda _context: None)
    return events


async def test_run_bot_sends_pong_after_startup_and_before_polling(
    monkeypatch, tmp_path
):
    events = install_bot(monkeypatch, tmp_path)

    await run_bot()

    assert events.index("controller-start") < events.index(("send", 42, "pong"))
    assert events.index(("send", 42, "pong")) < events.index("polling-start")


async def test_run_bot_continues_polling_if_startup_pong_fails(
    monkeypatch, tmp_path, caplog
):
    events = install_bot(monkeypatch, tmp_path, TelegramError("unavailable"))

    await run_bot()

    assert "polling-start" in events
    assert "startup notification failed" in caplog.text
