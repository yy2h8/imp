"""state.db: schema migrations and the kv/queue repositories."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from assistant.db import (
    STATE_DB_NAME,
    jobs_meta_get,
    jobs_meta_list,
    jobs_meta_running,
    jobs_meta_update,
    jobs_meta_upsert,
    kv_get,
    kv_set,
    memory_all,
    memory_delete,
    memory_digest,
    memory_set,
    open_db,
    queue_claim_next,
    queue_count_waiting,
    queue_finish,
    queue_interrupted,
    queue_push,
    transcript_search,
    turn_insert,
    turns_report,
)


@pytest.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / STATE_DB_NAME)
    yield conn
    await conn.close()


async def test_kv_round_trip(db):
    assert await kv_get(db, "fingerprint") is None
    await kv_set(db, "fingerprint", "abc")
    assert await kv_get(db, "fingerprint") == "abc"
    await kv_set(db, "fingerprint", "xyz")  # upsert
    assert await kv_get(db, "fingerprint") == "xyz"


async def test_queue_fifo_order_and_states(db):
    first = await queue_push(db, "text", "hello")
    await queue_push(db, "text", "world")
    assert await queue_count_waiting(db) == 2
    assert await queue_claim_next(db) == (first, "text", "hello")
    assert await queue_count_waiting(db) == 1
    second, _, payload = await queue_claim_next(db)
    assert payload == "world"
    assert await queue_claim_next(db) is None  # nothing waiting
    await queue_finish(db, first)
    assert await queue_count_waiting(db) == 0
    assert second != first


async def test_queue_interrupted_reports_and_deletes_active(db):
    row_id = await queue_push(db, "text", "doomed")
    await queue_claim_next(db)  # marks it active
    interrupted = await queue_interrupted(db)
    assert interrupted == [(row_id, "text", "doomed")]
    assert await queue_interrupted(db) == []  # deleted, never replayed


async def test_open_db_migrations_idempotent(tmp_path):
    path = tmp_path / STATE_DB_NAME
    conn = await open_db(path)
    version = (await conn.execute_fetchall("PRAGMA user_version"))[0][0]
    await conn.close()
    conn = await open_db(path)  # second open: same version, no error
    version_again = (await conn.execute_fetchall("PRAGMA user_version"))[0][0]
    assert version == version_again == 1
    await conn.close()


async def test_concurrent_writers_do_not_lock(tmp_path):
    path = tmp_path / STATE_DB_NAME
    conn_a = await open_db(path)
    conn_b = await open_db(path)

    def sync_writes():
        rows = []
        for i in range(50):
            conn = sqlite3.connect(path, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                "INSERT INTO kv(key, value) VALUES (?, ?)",
                (f"sync-{i}", str(i)),
            )
            conn.commit()
            conn.close()
            rows.append(i)
        return rows

    async def async_writes():
        for i in range(50):
            await kv_set(conn_a, f"async-{i}", str(i))

    sync_rows = await asyncio.to_thread(sync_writes)
    await asyncio.gather(async_writes(), kv_set(conn_b, "b", "1"))
    assert len(sync_rows) == 50
    assert await kv_get(conn_b, "async-49") == "49"
    assert await kv_get(conn_a, "sync-49") == "49"
    await conn_a.close()
    await conn_b.close()


async def test_memory_round_trip_and_order(db):
    await memory_set(db, "server", "cubie")
    await memory_set(db, "units", "metric")
    assert await memory_all(db) == [("server", "cubie"), ("units", "metric")]
    assert await memory_delete(db, "server") is True
    assert await memory_delete(db, "server") is False
    assert await memory_all(db) == [("units", "metric")]


async def test_memory_value_cap(db):
    with pytest.raises(ValueError, match="2048"):
        await memory_set(db, "big", "x" * 2049)
    await memory_set(db, "max", "x" * 2048)  # boundary is fine
    assert (await memory_all(db))[0][1] == "x" * 2048


async def test_memory_key_count_cap(db):
    from assistant.db import MAX_MEMORY_KEYS

    for i in range(MAX_MEMORY_KEYS):
        await memory_set(db, f"k{i}", str(i))
    with pytest.raises(ValueError, match="200"):
        await memory_set(db, "extra", "boom")


async def test_memory_digest_truncates_values_and_caps_total(db):
    await memory_set(db, "a", "short")
    await memory_set(db, "b", "y" * 300)  # digest truncates to 120 chars
    digest = await memory_digest(db)
    assert "a: short" in digest
    assert ("b: " + "y" * 120) in digest
    assert "y" * 121 not in digest
    from assistant.db import MEMORY_DIGEST_CHARS

    assert len(digest) <= MEMORY_DIGEST_CHARS
    # many entries: newest kept, overflow counted
    for i in range(60):
        await memory_set(db, f"n{i:02d}", f"value {i:02d} " + "z" * 100)
    digest = await memory_digest(db)
    assert len(digest) <= MEMORY_DIGEST_CHARS
    assert "more memories" in digest
    assert "n59" in digest  # newest survives


async def test_turn_insert_and_report_periods(db):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old = now - timedelta(days=40)
    await turn_insert(
        db, ts=old.isoformat(), kind="interactive", session_id="s1",
        model="m", in_tokens=100, out_tokens=50, cost_usd=0.5,
        tools=2, seconds=10.0, ok=True,
    )
    await turn_insert(
        db, ts=now.isoformat(), kind="job", session_id="s2",
        model="m", in_tokens=10, out_tokens=5, cost_usd=0.01,
        tools=1, seconds=2.0, ok=False,
    )
    report = await turns_report(db, "all")
    assert report == {
        "turns": 2, "in_tokens": 110, "out_tokens": 55, "cost_usd": 0.51
    }
    day = await turns_report(db, "day")
    assert day["turns"] == 1 and day["cost_usd"] == 0.01
    week = await turns_report(db, "week")
    assert week["turns"] == 1
    month = await turns_report(db, "month")
    assert month["turns"] == 1 and month["in_tokens"] == 10


async def test_jobs_meta_cycle(db):
    await jobs_meta_upsert(
        db, schedule_id="daily", label="Daily report", prompt="report",
        tz="Asia/Almaty", state="scheduled",
    )
    stored = await jobs_meta_get(db, "daily")
    assert stored["label"] == "Daily report"
    assert stored["result"] == "" and stored["delivery"] == ""
    await jobs_meta_update(db, "daily", state="running")
    running = await jobs_meta_running(db)
    assert [row["schedule_id"] for row in running] == ["daily"]
    # running scan flips state to interrupted and reports once
    assert await jobs_meta_running(db) == []
    interrupted = await jobs_meta_get(db, "daily")
    assert interrupted["state"] == "interrupted"
    await jobs_meta_update(db, "daily", result="done text", delivery="sent")
    listed = await jobs_meta_list(db)
    assert listed[0]["result"] == "done text"
    assert await jobs_meta_get(db, "missing") is None


async def test_transcript_search(db):
    import json

    db.row_factory = None
    await db.execute(
        "INSERT INTO transcripts(session_id, seq, ts, message) VALUES "
        "(?, ?, ?, ?)",
        ("s1", 0, "2026-01-01T00:00:00+00:00",
         json.dumps({"role": "user", "content": "deploy the armbian box"})),
    )
    await db.execute(
        "INSERT INTO transcripts(session_id, seq, ts, message) VALUES "
        "(?, ?, ?, ?)",
        ("s2", 0, "2026-01-02T00:00:00+00:00",
         json.dumps({"role": "user", "content": "unrelated note"})),
    )
    await db.commit()
    hits = await transcript_search(db, "armbian")
    assert len(hits) == 1 and hits[0][0] == "s1"
    assert "armbian" in hits[0][1]
    assert await transcript_search(db, "absent") == []
    for i in range(15):
        await db.execute(
            "INSERT INTO transcripts(session_id, seq, ts, message) VALUES "
            "(?, ?, ?, ?)",
            (f"n{i}", 0, "2026-01-03T00:00:00+00:00",
             json.dumps({"role": "user", "content": f"armbian {i}"})),
        )
    await db.commit()
    assert len(await transcript_search(db, "armbian")) == 10  # default limit
