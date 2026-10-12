from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from assistant.db import (
    STATE_DB_NAME,
    delivery_claim,
    delivery_recover,
    delivery_retry,
    delivery_sent,
    open_db,
    queue_push,
    recover_runs,
    turn_checkpoint,
    turn_finish,
    turn_start,
)


@pytest.fixture
async def store(tmp_path):
    path = tmp_path / STATE_DB_NAME
    db = await open_db(path)
    yield path, db
    await db.close()


async def test_finish_is_atomic_and_keeps_recurring_results(store):
    path, db = store
    queue_id = await queue_push(db, "text", "{}")
    first = await turn_start(
        path, kind="interactive", model="m", session_id="s", queue_id=queue_id
    )
    await turn_checkpoint(
        path,
        first,
        session_id="s",
        in_tokens=20,
        out_tokens=4,
        cost_usd=0.01,
        cost_complete=False,
        tools=2,
        seconds=3,
    )
    await turn_finish(
        path,
        first,
        status="completed",
        error_code=None,
        error_message=None,
        chat_id=42,
        text="answer",
    )
    await turn_finish(
        path,
        first,
        status="completed",
        error_code=None,
        error_message=None,
        chat_id=42,
        text="duplicate",
    )
    assert await db.execute_fetchall("SELECT status,ok,cost_complete FROM turns") == [
        ("completed", 1, 0)
    ]
    assert await db.execute_fetchall("SELECT text,state FROM deliveries") == [
        ("answer", "pending")
    ]
    assert await db.execute_fetchall("SELECT id FROM queue") == []
    for text in ("day one", "day two"):
        run = await turn_start(
            path, kind="job", model="m", session_id="s", schedule_id="daily"
        )
        await turn_finish(
            path,
            run,
            status="completed",
            error_code=None,
            error_message=None,
            chat_id=42,
            text=text,
            job_state="scheduled",
        )
    assert len(await db.execute_fetchall("SELECT id FROM deliveries")) == 3


async def test_failed_enqueue_rolls_back_completion(store):
    path, db = store
    queue_id = await queue_push(db, "text", "{}")
    run = await turn_start(
        path, kind="interactive", model="m", session_id="s", queue_id=queue_id
    )
    await db.execute(
        "CREATE TRIGGER reject_delivery BEFORE INSERT ON deliveries BEGIN SELECT RAISE(ABORT,'probe'); END"
    )
    await db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        await turn_finish(
            path,
            run,
            status="completed",
            error_code=None,
            error_message=None,
            chat_id=42,
            text="answer",
        )
    assert await db.execute_fetchall("SELECT status FROM turns") == [("running",)]
    assert len(await db.execute_fetchall("SELECT id FROM queue")) == 1


async def test_recovery_records_interruptions_once(store):
    path, db = store
    queue_id = await queue_push(db, "text", "{}")
    await db.execute("UPDATE queue SET state='active'")
    await db.commit()
    run = await turn_start(
        path, kind="interactive", model="m", session_id="s", queue_id=queue_id
    )
    await turn_checkpoint(
        path,
        run,
        session_id="s",
        in_tokens=12,
        out_tokens=3,
        cost_usd=0.02,
        cost_complete=False,
        tools=1,
        seconds=4,
    )
    await recover_runs(path, 42)
    await recover_runs(path, 42)
    assert await db.execute_fetchall(
        "SELECT status,error_code,seconds,cost_usd FROM turns"
    ) == [("interrupted", "interrupted", 4, 0.02)]
    assert len(await db.execute_fetchall("SELECT id FROM deliveries")) == 1
    assert await db.execute_fetchall("SELECT id FROM queue") == []


async def test_delivery_claim_retry_and_restart(store):
    path, db = store
    run = await turn_start(path, kind="job", model="m", session_id="s")
    await turn_finish(
        path,
        run,
        status="completed",
        error_code=None,
        error_message=None,
        chat_id=42,
        text="result",
    )
    now = datetime.now(UTC).isoformat()
    row = await delivery_claim(path, now)
    assert row["chat_id"] == 42 and row["attempts"] == 1
    assert await delivery_claim(path, now) is None
    await delivery_recover(path)
    row = await delivery_claim(path, now)
    assert row["attempts"] == 2
    await delivery_retry(path, row["id"], error="network", next_attempt_at="2099-01-01")
    assert await delivery_claim(path, now) is None
    row = await delivery_claim(path, "2099-02-01")
    await delivery_sent(path, row["id"], [11, 12])
    await delivery_recover(path)
    assert await delivery_claim(path, "2099-02-01") is None
    assert await db.execute_fetchall("SELECT state,message_ids FROM deliveries") == [
        ("sent", "[11, 12]")
    ]


async def test_report_separates_running_and_partial_cost(store):
    from assistant.db import turns_report

    path, db = store
    run = await turn_start(path, kind="job", model="m", session_id="s")
    await turn_checkpoint(
        path,
        run,
        session_id="s",
        in_tokens=12,
        out_tokens=3,
        cost_usd=0.02,
        cost_complete=False,
        tools=1,
        seconds=4,
    )
    report = await turns_report(db, "all")
    assert report["running"] == 1 and report["completed"] == 0
    assert report["incomplete_cost"] == 1 and report["cost_usd"] == 0.02


async def test_v2_migration_preserves_history_and_does_not_replay_queued(tmp_path):
    from assistant.db import _SCHEMA_V1, SCHEMA_VERSION

    path = tmp_path / STATE_DB_NAME
    with sqlite3.connect(path) as raw:
        raw.executescript(_SCHEMA_V1)
        raw.execute("ALTER TABLE queue ADD COLUMN album_id TEXT")
        raw.execute("PRAGMA user_version=2")
        raw.execute(
            "INSERT INTO turns VALUES (17,'2026-01-01','job','old','m',10,2,.01,0,1,1)"
        )
        raw.execute(
            "INSERT INTO jobs_meta VALUES ('old','old','p','UTC','done','answer','queued','old','2026-01-01')"
        )
    db = await open_db(path)
    try:
        assert await db.execute_fetchall("PRAGMA user_version") == [(SCHEMA_VERSION,)]
        assert await db.execute_fetchall(
            "SELECT id,status,ok,cost_usd,cost_complete FROM turns"
        ) == [(17, "legacy", 1, 0.01, None)]
        await recover_runs(path, 42)
        assert await db.execute_fetchall("SELECT id FROM deliveries") == []
        assert await db.execute_fetchall("PRAGMA integrity_check") == [("ok",)]
    finally:
        await db.close()


async def test_cleanup_clears_only_old_sent_payloads(store):
    from assistant.db import delivery_sent, prune_transcripts

    path, db = store
    for text in ("old sent", "old pending"):
        run = await turn_start(path, kind="job", model="m", session_id="s")
        await turn_finish(
            path,
            run,
            status="completed",
            error_code=None,
            error_message=None,
            chat_id=42,
            text=text,
        )
    first = await delivery_claim(path, datetime.now(UTC).isoformat())
    await delivery_sent(path, first["id"], [1])
    await db.execute(
        "UPDATE deliveries SET created_at='2000-01-01',sent_at=CASE WHEN state='sent' THEN '2000-01-01' ELSE NULL END"
    )
    await db.commit()
    await prune_transcripts(db, 7, 90)
    assert await db.execute_fetchall(
        "SELECT text,state,message_ids FROM deliveries ORDER BY id"
    ) == [("", "sent", "[1]"), ("old pending", "pending", None)]
    assert not db.in_transaction


async def test_returned_usage_is_checkpointed_before_reasoning_can_interrupt(store):
    from test_agent import StubClient, message_item, reasoning_item, response, usage_ns

    from assistant.turns import Run
    from imp.agent import Agent, Context
    from imp.config import Config
    from imp.events import EventType

    path, db = store
    config = Config(api_key="test-key", workspace=path.parent)
    agent = Agent(
        config,
        {},
        StubClient(
            [
                response(
                    [reasoning_item(text="thinking"), message_item("answer")],
                    usage=usage_ns(10, 2, 12, 0.02),
                )
            ]
        ),
        Context(config, "sys"),
    )
    run = await Run.start(path, kind="interactive", model="m", session_id="s")
    events = agent.run_turn("hello")
    try:
        async for event in events:
            await run.observe(event)
            if event.type is EventType.REASONING:
                break
    finally:
        await events.aclose()
    await run.finish("interrupted", 42, "interrupted", code="interrupted")
    assert await db.execute_fetchall("SELECT in_tokens,cost_usd FROM turns") == [
        (10, 0.02)
    ]
