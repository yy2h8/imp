"""Integration tests for turn execution and Telegram message intake."""

from __future__ import annotations

import asyncio

import pytest
from test_agent import StubClient, message_item, response, usage_ns

from assistant.adapters.ui import turn_summary
from assistant.app import AskRouter, AssistantApp, Session
from assistant.config import AssistantConfig
from assistant.db import STATE_DB_NAME, open_db
from assistant.main import AssistantController, TurnRunner
from assistant.outbox import Outbox
from assistant.uploads import Uploads
from imp.agent import Agent
from imp.config import Config


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.actions: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append(text)
        return len(self.sent)

    async def send_text(self, chat_id: int, text: str) -> list[int]:
        self.sent.append(text)
        return [len(self.sent)]

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append(text)
        return True

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append(action)

    async def download(self, file_id: str):
        yield b"file contents"


@pytest.fixture
async def app(tmp_path):
    config = Config(api_key="k", workspace=tmp_path)
    assistant_config = AssistantConfig(
        bot_token="123456:TEST-TOKEN",
        allowed_user_ids=frozenset({7}),
        home=tmp_path,
    )
    db = await open_db(tmp_path / STATE_DB_NAME)
    session = Session.open(config, "system prompt", tmp_path / STATE_DB_NAME)
    client = StubClient([])
    agent = Agent(config=config, tools={}, client=client, context=session.context)
    bot = FakeBot()
    outbox = Outbox(bot.send_text)
    instance = AssistantApp(
        config=config,
        assistant=assistant_config,
        agent=agent,
        session=session,
        bot=bot,
        chat_id=7,
        ask_router=AskRouter(),
        uploads=Uploads(bot=bot, inbox=tmp_path / "inbox", chat_id=7),
        db=db,
        outbox=outbox,
        jobs_semaphore=asyncio.Semaphore(2),
    )
    try:
        yield instance
    finally:
        instance.session.writer.__exit__(None, None, None)
        await db.close()


def owner_message(text: str, message_id: int = 1, user_id: int = 7) -> dict:
    return {
        "message_id": message_id,
        "from": {"id": user_id},
        "chat": {"id": user_id, "type": "private"},
        "text": text,
    }


async def test_turn_summary_and_cost_are_recorded(app):
    app.agent.client = StubClient(
        [response([message_item("answer")], usage=usage_ns(20, 8, 28, 0.0042))]
    )
    await TurnRunner(app, app.bot, 0, 3500).run("hello")
    rows = await app.db.execute_fetchall(
        "SELECT kind, in_tokens, out_tokens, cost_usd, ok FROM turns"
    )
    assert len(rows) == 1
    assert tuple(rows[0]) == ("interactive", 20, 8, 0.0042, 1)
    assert "✓ done · 0 tools ·" in app.bot.edits[-1]
    assert "$0.0042" in app.bot.edits[-1]
    assert app.bot.sent[-1] == "answer"
    assert turn_summary(3, 47, True, 0.0134) == "✓ done · 3 tools · 47 s · $0.0134"


async def test_turn_records_tokens_even_when_provider_omits_cost(app):
    app.agent.client = StubClient(
        [response([message_item("answer")], usage=usage_ns(20, 8, 28))]
    )
    await TurnRunner(app, app.bot, 0, 3500).run("hello")
    row = await app.db.execute_fetchall(
        "SELECT in_tokens, out_tokens, cost_usd FROM turns"
    )
    assert tuple(row[0]) == (20, 8, None)
    assert "$" not in app.bot.edits[-1]


async def test_controller_enqueues_owner_message_and_runs_turn(app):
    app.agent.client = StubClient([response([message_item("done")])])
    controller = AssistantController(app)
    await controller.handle_message(owner_message("do work"))
    await controller.wait_idle()
    rows = await app.db.execute_fetchall("SELECT kind FROM turns")
    assert rows[0][0] == "interactive"
    assert app.bot.sent[-1] == "done"
    await controller.close()


async def test_controller_deduplicates_redelivered_message(app):
    app.agent.client = StubClient([response([message_item("once")])])
    controller = AssistantController(app)
    msg = owner_message("do work", message_id=9)
    await controller.handle_message(msg)
    await controller.handle_message(msg)
    await controller.wait_idle()
    rows = await app.db.execute_fetchall("SELECT COUNT(*) FROM turns")
    assert rows[0][0] == 1
    await controller.close()


async def test_controller_ignores_non_owner_and_answers_question(app):
    controller = AssistantController(app)
    await controller.handle_message(owner_message("ignore", user_id=99))
    future = app.ask_router.start()
    await controller.handle_message(owner_message("answer", message_id=2))
    assert future.result() == "answer"
    assert await app.db.execute_fetchall("SELECT COUNT(*) FROM queue") == [(0,)]
    app.ask_router.clear()
    await controller.close()


async def test_controller_acknowledges_work_queued_during_active_turn(app):
    app.agent.client = StubClient([response([message_item("queued answer")])])
    app.turn_state["active"] = True
    controller = AssistantController(app)
    await controller.handle_message(owner_message("wait for me", message_id=12))
    assert "Принято — в очереди" in app.bot.sent[0]
    await controller.wait_idle()
    app.turn_state["active"] = False
    await controller.close()


async def test_worker_rechecks_queue_after_empty_poll_race(app, monkeypatch):
    from assistant.db import queue_claim_next, queue_count_waiting

    original = queue_claim_next
    empty_claimed = asyncio.Event()
    release_empty = asyncio.Event()
    first = True

    async def pause_after_empty(conn):
        nonlocal first
        row = await original(conn)
        if row is None and first:
            first = False
            empty_claimed.set()
            await release_empty.wait()
        return row

    monkeypatch.setattr("assistant.main.queue_claim_next", pause_after_empty)
    app.agent.client = StubClient([response([message_item("arrived in gap")])])
    controller = AssistantController(app)
    await controller.start()
    await asyncio.wait_for(empty_claimed.wait(), timeout=1)
    await controller.handle_message(owner_message("late request", message_id=30))
    release_empty.set()
    await controller.wait_idle()
    assert await queue_count_waiting(app.db) == 0
    assert app.bot.sent[-1] == "arrived in gap"
    await controller.close()


async def test_command_status_and_new_are_direct_not_model_turns(app):
    controller = AssistantController(app)
    await controller.handle_message(owner_message("/status", message_id=21))
    await controller.wait_idle()
    assert "Контекст" in app.bot.sent[-1]
    assert "(оценка)" in app.bot.sent[-1]
    previous = app.session.writer.name
    await controller.handle_message(owner_message("/new", message_id=22))
    await controller.wait_idle()
    assert "Started a fresh session" in app.bot.sent[-1]
    assert app.session.writer.name != previous
    assert await app.db.execute_fetchall("SELECT COUNT(*) FROM turns") == [(0,)]
    await controller.close()
