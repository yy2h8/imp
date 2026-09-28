"""state.db: one SQLite file (WAL) holding every assistant table.

Our tables live here (kv, memory, queue, turns, transcripts, jobs_meta);
APScheduler's ``apscheduler_jobs`` table shares the file through its own
SQLAlchemyJobStore connection — table names do not collide. Migrations run
by ``PRAGMA user_version``: each version's DDL is idempotent, applied once.
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite

STATE_DB_NAME = "state.db"

# Every connection to state.db — ours and APScheduler's engine — must use
# WAL plus this timeout so three writer families never raise "database is
# locked" against each other.
BUSY_TIMEOUT_MS = 30000

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT 'waiting',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_state_id ON queue(state, id);
CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    session_id  TEXT NOT NULL,
    model       TEXT NOT NULL,
    in_tokens   INTEGER NOT NULL,
    out_tokens  INTEGER NOT NULL,
    cost_usd    REAL,
    tools       INTEGER NOT NULL,
    seconds     REAL NOT NULL,
    ok          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_turns_kind_ts ON turns(kind, ts);
CREATE TABLE IF NOT EXISTS transcripts (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    message    TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS jobs_meta (
    schedule_id TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    prompt      TEXT NOT NULL,
    tz          TEXT NOT NULL,
    state       TEXT NOT NULL,
    result      TEXT NOT NULL DEFAULT '',
    delivery    TEXT NOT NULL DEFAULT '',
    transcript  TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL
);
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1}


async def open_db(path: Path) -> aiosqlite.Connection:
    """Open (and migrate if needed) state.db with WAL and busy timeout."""
    conn = await aiosqlite.connect(path)
    try:
        await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        await conn.execute("PRAGMA journal_mode=WAL")
        rows = await conn.execute_fetchall("PRAGMA user_version")
        version = rows[0][0] if rows else 0
        for target in sorted(_MIGRATIONS):
            if target > version:
                await conn.executescript(_MIGRATIONS[target])
                await conn.execute(f"PRAGMA user_version={target}")
                await conn.commit()
    except BaseException:
        await conn.close()
        raise
    return conn


async def kv_get(conn: aiosqlite.Connection, key: str) -> str | None:
    async with conn.execute("SELECT value FROM kv WHERE key = ?", (key,)) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def kv_set(conn: aiosqlite.Connection, key: str, value: str) -> None:
    await conn.execute(
        "INSERT INTO kv(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await conn.commit()


async def queue_push(conn: aiosqlite.Connection, kind: str, payload: str) -> int:
    from datetime import UTC, datetime

    cur = await conn.execute(
        "INSERT INTO queue(kind, payload, state, created_at) "
        "VALUES (?, ?, 'waiting', ?)",
        (kind, payload, datetime.now(UTC).isoformat()),
    )
    await conn.commit()
    return cur.lastrowid


async def queue_claim_next(
    conn: aiosqlite.Connection,
) -> tuple[int, str, str] | None:
    """Oldest waiting row → active; returns (id, kind, payload) or None."""
    async with conn.execute(
        "SELECT id, kind, payload FROM queue WHERE state = 'waiting' "
        "ORDER BY id LIMIT 1"
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    await conn.execute("UPDATE queue SET state = 'active' WHERE id = ?", (row[0],))
    await conn.commit()
    return row[0], row[1], row[2]


async def queue_count_waiting(conn: aiosqlite.Connection) -> int:
    async with conn.execute(
        "SELECT COUNT(*) FROM queue WHERE state = 'waiting'"
    ) as cur:
        (count,) = await cur.fetchone()
    return count


async def queue_finish(conn: aiosqlite.Connection, row_id: int) -> None:
    await conn.execute("DELETE FROM queue WHERE id = ?", (row_id,))
    await conn.commit()


async def queue_interrupted(
    conn: aiosqlite.Connection,
) -> list[tuple[int, str, str]]:
    """All rows stuck active (a crash mid-turn): return and delete them —
    callers report the interruption, never replay the work."""
    async with conn.execute(
        "SELECT id, kind, payload FROM queue WHERE state = 'active' ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    await conn.execute("DELETE FROM queue WHERE state = 'active'")
    await conn.commit()
    return [(row[0], row[1], row[2]) for row in rows]
