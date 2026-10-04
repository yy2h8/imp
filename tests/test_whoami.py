"""whoami: token identity + sender-id discovery (mocked bot, no network),
plus the __main__ command dispatch."""

from __future__ import annotations

import pytest

from assistant.main import main, whoami


class StubWhoamiBot:
    """get_me + scripted getUpdates pages; the poll ends when pages drain."""

    def __init__(self, pages: list[list[dict]]):
        self.pages = list(pages)
        self.me = {"username": "my_bot", "first_name": "My Bot"}
        self.closed = False

    async def initialize(self) -> None:
        pass

    async def get_me(self) -> dict:
        return self.me

    async def get_updates(self, offset: int) -> list[dict]:
        if not self.pages:  # whoami long-polls forever; end it here
            raise KeyboardInterrupt()
        return self.pages.pop(0)

    async def close(self) -> None:
        self.closed = True


def install(monkeypatch, bot: StubWhoamiBot) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr("assistant.main.TelegramBot", lambda token: bot)


def sender_update(user_id: int, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "message": {"from": {"id": user_id, "first_name": "Ilyas"}},
    }


async def test_whoami_prints_bot_and_sender_ids(monkeypatch, capsys):
    bot = StubWhoamiBot([[sender_update(111, 1), sender_update(222, 2)], []])
    install(monkeypatch, bot)

    with pytest.raises(KeyboardInterrupt):
        await whoami()

    out = capsys.readouterr().out
    assert "@my_bot" in out
    assert "id: 111" in out and "id: 222" in out
    assert "Ilyas" in out
    assert bot.closed  # the transport is closed on exit


def test_whoami_requires_the_token(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        __import__("asyncio").run(whoami())


def test_main_whoami_subcommand(monkeypatch):
    called = {}

    def fake_whoami():
        called["whoami"] = True

    def fake_run(coro):
        if coro is not None:
            coro.close()  # never awaited: close the coroutine cleanly
        called["run"] = True

    monkeypatch.setattr("assistant.main.whoami", fake_whoami)
    monkeypatch.setattr("assistant.main.asyncio.run", fake_run)

    main(["whoami"])
    assert called.get("whoami") is True and called.get("run") is True


def test_main_default_runs_the_bot(monkeypatch):
    called = {}

    def fake_run(coro):
        called["bot"] = True
        coro.close()

    async def stub_run_bot():
        raise NotImplementedError  # never awaited: run() is faked

    monkeypatch.setattr("assistant.main.run_bot", stub_run_bot)
    monkeypatch.setattr("assistant.main.asyncio.run", fake_run)

    main([])
    assert called.get("bot") is True


def test_main_refuses_unknown_commands():
    with pytest.raises(SystemExit, match="unknown command"):
        main(["frobnicate"])
