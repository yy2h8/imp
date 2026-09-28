"""state.db: one SQLite file (WAL) holding every assistant table.

Our tables live here (kv, memory, queue, turns, transcripts, jobs_meta);
APScheduler's ``apscheduler_jobs`` table shares the file through its own
SQLAlchemyJobStore connection — table names do not collide. Migrations run
by ``PRAGMA user_version``: each version's DDL is idempotent, applied once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiosqlite

STATE_DB_NAME = "state.db"

MAX_MEMORY_KEYS = 200
MAX_MEMORY_VALUE_CHARS = 2048
MEMORY_DIGEST_CHARS = 1500
_MEMORY_DIGEST_VALUE_CHARS = 120

_PERIODS: dict[str, timedelta | None] = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "all": None,
}

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


async def memory_set(conn: aiosqlite.Connection, key: str, value: str) -> None:
    if len(value) > MAX_MEMORY_VALUE_CHARS:
        raise ValueError(f"memory value exceeds {MAX_MEMORY_VALUE_CHARS} chars")
    async with conn.execute("SELECT COUNT(*) FROM memory") as cur:
        (count,) = await cur.fetchone()
        exists = await kv_row_exists(conn, "memory", key)
    if count >= MAX_MEMORY_KEYS and not exists:
        raise ValueError(f"memory holds at most {MAX_MEMORY_KEYS} keys")
    await conn.execute(
        "INSERT INTO memory(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, datetime.now(UTC).isoformat()),
    )
    await conn.commit()


async def kv_row_exists(
    conn: aiosqlite.Connection, table: str, key: str
) -> bool:
    async with conn.execute(
        f"SELECT 1 FROM {table} WHERE key = ?",  # table name fixed by caller
        (key,),
    ) as cur:
        return await cur.fetchone() is not None


async def memory_delete(conn: aiosqlite.Connection, key: str) -> bool:
    cur = await conn.execute("DELETE FROM memory WHERE key = ?", (key,))
    await conn.commit()
    return cur.rowcount > 0


async def memory_all(conn: aiosqlite.Connection) -> list[tuple[str, str]]:
    """(key, value), oldest-updated first."""
    async with conn.execute(
        "SELECT key, value FROM memory ORDER BY updated_at, key"
    ) as cur:
        return [(row[0], row[1]) for row in await cur.fetchall()]


async def memory_digest(conn: aiosqlite.Connection) -> str:
    """Compact one-line-per-entry digest, newest entries kept, capped at
    MEMORY_DIGEST_CHARS with an overflow marker. Values truncated, never the
    stored data."""
    entries = list(reversed(await memory_all(conn)))  # newest first
    lines: list[str] = []
    dropped = 0
    for key, value in entries:
        line = f"{key}: {value[:_MEMORY_DIGEST_VALUE_CHARS]}"
        candidate = "\n".join(lines + [line])
        if len(candidate) > MEMORY_DIGEST_CHARS:
            dropped = len(entries) - len(lines)
            break
        lines.append(line)
    if dropped:
        overflow = f"… +{dropped} more memories"
        budget = MEMORY_DIGEST_CHARS - len(overflow) - 1
        while "\n".join(lines) and len("\n".join(lines)) > budget:
            lines.pop()
            dropped += 1
        lines.append(overflow)
    return "\n".join(lines)


async def turn_insert(
    conn: aiosqlite.Connection,
    *,
    ts: str,
    kind: str,
    session_id: str,
    model: str,
    in_tokens: int,
    out_tokens: int,
    cost_usd: float | None,
    tools: int,
    seconds: float,
    ok: bool,
) -> None:
    await conn.execute(
        "INSERT INTO turns(ts, kind, session_id, model, in_tokens, "
        "out_tokens, cost_usd, tools, seconds, ok) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ts, kind, session_id, model, in_tokens, out_tokens, cost_usd,
         tools, seconds, int(ok)),
    )
    await conn.commit()


async def turns_report(conn: aiosqlite.Connection, period: str) -> dict:
    """Aggregate over turns newer than the period boundary (UTC)."""
    window = _PERIODS.get(period)
    if window is None and period != "all":
        raise ValueError(f"unknown period: {period!r}")
    since = (
        datetime.now(UTC) - window
        if window is not None
        else datetime.min.replace(tzinfo=UTC)
    )
    async with conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(in_tokens), 0), "
        "COALESCE(SUM(out_tokens), 0), COALESCE(SUM(cost_usd), 0.0) "
        "FROM turns WHERE ts >= ?",
        (since.isoformat(),),
    ) as cur:
        turns, in_tokens, out_tokens, cost = await cur.fetchone()
    return {
        "turns": turns,
        "in_tokens": in_tokens,
        "out_tokens": out_tokens,
        "cost_usd": cost,
    }


async def jobs_meta_upsert(
    conn: aiosqlite.Connection,
    *,
    schedule_id: str,
    label: str,
    prompt: str,
    tz: str,
    state: str,
    result: str = "",
    delivery: str = "",
    transcript: str = "",
) -> None:
    await conn.execute(
        "INSERT INTO jobs_meta(schedule_id, label, prompt, tz, state, "
        "result, delivery, transcript, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(schedule_id) DO UPDATE SET label = excluded.label, "
        "prompt = excluded.prompt, tz = excluded.tz, state = excluded.state, "
        "result = excluded.result, delivery = excluded.delivery, "
        "transcript = excluded.transcript, "
        "updated_at = excluded.updated_at",
        (schedule_id, label, prompt, tz, state, result, delivery, transcript,
         datetime.now(UTC).isoformat()),
    )
    await conn.commit()


async def jobs_meta_get(
    conn: aiosqlite.Connection, schedule_id: str
) -> dict | None:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM jobs_meta WHERE schedule_id = ?", (schedule_id,)
    ) as cur:
        row = await cur.fetchone()
    return dict(row) if row else None


async def jobs_meta_list(conn: aiosqlite.Connection) -> list[dict]:
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM jobs_meta ORDER BY schedule_id"
    ) as cur:
        return [dict(row) for row in await cur.fetchall()]


async def jobs_meta_update(
    conn: aiosqlite.Connection, schedule_id: str, **fields: str
) -> None:
    if not fields:
        return
    columns = ", ".join(f"{name} = ?" for name in fields)
    await conn.execute(
        f"UPDATE jobs_meta SET {columns}, updated_at = ? "  # columns from kwargs
        "WHERE schedule_id = ?",
        (*fields.values(), datetime.now(UTC).isoformat(), schedule_id),
    )
    await conn.commit()


async def jobs_meta_running(conn: aiosqlite.Connection) -> list[dict]:
    """Rows stuck state='running' (a crash mid-job): report once, then mark
    'interrupted' so a second scan is empty."""
    conn.row_factory = aiosqlite.Row
    async with conn.execute(
        "SELECT * FROM jobs_meta WHERE state = 'running' ORDER BY schedule_id"
    ) as cur:
        rows = [dict(row) for row in await cur.fetchall()]
    if rows:
        await conn.execute(
            "UPDATE jobs_meta SET state = 'interrupted', updated_at = ? "
            "WHERE state = 'running'",
            (datetime.now(UTC).isoformat(),),
        )
        await conn.commit()
    return rows


async def transcript_search(
    conn: aiosqlite.Connection, query: str, limit: int = 10
) -> list[tuple[str, str]]:
    """(session_id, message JSON) rows containing query, newest first.
    LIKE '%q%' cannot use an index — a deliberate scan at personal scale."""
    async with conn.execute(
        "SELECT session_id, message FROM transcripts "
        "WHERE message LIKE '%' || ? || '%' "
        "ORDER BY ts DESC, session_id DESC, seq DESC LIMIT ?",
        (query, limit),
    ) as cur:
        return [(row[0], row[1]) for row in await cur.fetchall()]
