"""state.db: schema migrations and the kv/queue repositories."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from assistant.db import (
    STATE_DB_NAME,
    kv_get,
    kv_set,
    open_db,
    queue_claim_next,
    queue_count_waiting,
    queue_finish,
    queue_interrupted,
    queue_push,
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
