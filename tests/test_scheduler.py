"""APScheduler-driven job execution: durable schedules, concurrent runs,
outbox delivery, interruption recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from apscheduler.triggers.date import DateTrigger
from test_agent import StubClient, message_item, response, usage_ns

from assistant.db import (
    STATE_DB_NAME,
    jobs_meta_get,
    jobs_meta_running,
    jobs_meta_upsert,
    open_db,
)
from assistant.scheduler import (
    JobContext,
    build_scheduler,
    run_scheduled_job,
    set_context,
    startup_recovery,
)
from imp.config import Config


class FakeOutbox:
    def __init__(self) -> None:
        self.items: list[str] = []

    async def submit(self, text: str) -> None:
        self.items.append(text)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, chat_id: int, text: str) -> list[int]:
        self.sent.append(text)
        return [1]


class FakeAgent:
    def __init__(self, script: list) -> None:
        self.tools: dict = {}
        self.client = StubClient(script)


class FakeApp:
    def __init__(self, tmp_path: Path, script: list) -> None:
        self.config = Config(api_key="k", workspace=tmp_path)
        self.agent = FakeAgent(script)
        self.chat_id = 7
        self.bot = FakeBot()

    def build_prompt(self) -> str:
        return "job system prompt"


async def test_date_job_fires_records_and_delivers(tmp_path):
    db_path = tmp_path / STATE_DB_NAME
    conn = await open_db(db_path)
    app = FakeApp(
        tmp_path,
        [response([message_item("valuable result")], usage=usage_ns(10, 5, 15, 0.01))],
    )
    outbox = FakeOutbox()
    scheduler = build_scheduler(tmp_path, "UTC")
    scheduler.start()
    ctx = JobContext(
        app=app,
        outbox=outbox,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(2),
        db=conn,
        db_path=db_path,
        scheduler=scheduler,
    )
    set_context(ctx)
    try:
        await jobs_meta_upsert(
            conn, schedule_id="j1", label="job one", prompt="prompt…",
            tz="UTC", state="scheduled",
        )
        scheduler.add_job(
            run_scheduled_job,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=1)),
            args=["j1", "prompt…"],
            id="j1",
            replace_existing=True,
        )
        for _ in range(100):
            if outbox.items:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        assert outbox.items[0].startswith("⏰ j1\n\nvaluable result\n\n")
        assert "✓ done · 0 tools ·" in outbox.items[0]
        assert "$0.0100" in outbox.items[0]
        meta = await jobs_meta_get(conn, "j1")
        assert meta["state"] == "done"  # one-shot: not rescheduled
        assert meta["result"] == "valuable result"
        assert meta["transcript"]  # session id recorded
        rows = await conn.execute_fetchall(
            "SELECT kind, model, in_tokens, out_tokens, cost_usd, ok FROM turns"
        )
        assert len(rows) == 1
        kind, model, in_tok, out_tok, cost, ok = rows[0]
        assert (kind, in_tok, out_tok, cost, ok) == ("job", 10, 5, 0.01, 1)
        assert model
    finally:
        set_context(None)
        scheduler.shutdown(wait=False)
        await conn.close()


async def test_failing_job_marks_error_and_notifies(tmp_path):
    db_path = tmp_path / STATE_DB_NAME
    conn = await open_db(db_path)
    app = FakeApp(tmp_path, [RuntimeError("model exploded")])
    outbox = FakeOutbox()
    scheduler = build_scheduler(tmp_path, "UTC")
    scheduler.start()
    ctx = JobContext(
        app=app,
        outbox=outbox,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(2),
        db=conn,
        db_path=db_path,
        scheduler=scheduler,
    )
    set_context(ctx)
    try:
        await jobs_meta_upsert(
            conn, schedule_id="j2", label="job two", prompt="prompt…",
            tz="UTC", state="scheduled",
        )
        scheduler.add_job(
            run_scheduled_job,
            DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=1)),
            args=["j2", "prompt…"],
            id="j2",
            replace_existing=True,
        )
        for _ in range(100):
            if outbox.items:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)

        meta = await jobs_meta_get(conn, "j2")
        assert meta["state"] == "error"
        assert "failed" in meta["result"]
        assert outbox.items and outbox.items[0].startswith("⏰ j2")
        rows = await conn.execute_fetchall("SELECT ok FROM turns")
        assert rows[0][0] == 0
    finally:
        set_context(None)
        scheduler.shutdown(wait=False)
        await conn.close()


async def test_startup_recovery_reports_interrupted_jobs(tmp_path):
    db_path = tmp_path / STATE_DB_NAME
    conn = await open_db(db_path)
    app = FakeApp(tmp_path, [])
    outbox = FakeOutbox()
    scheduler = build_scheduler(tmp_path, "UTC")
    ctx = JobContext(
        app=app,
        outbox=outbox,  # type: ignore[arg-type]
        semaphore=asyncio.Semaphore(2),
        db=conn,
        db_path=db_path,
        scheduler=scheduler,
    )
    await jobs_meta_upsert(
        conn, schedule_id="stuck", label="l", prompt="p", tz="UTC",
        state="running",
    )
    await startup_recovery(ctx)
    assert len(app.bot.sent) == 1
    assert "stuck" in app.bot.sent[0]
    assert "nterrupted" in app.bot.sent[0]
    assert await jobs_meta_running(conn) == []  # reported once
    meta = await jobs_meta_get(conn, "stuck")
    assert meta["state"] == "interrupted"
    await conn.close()
