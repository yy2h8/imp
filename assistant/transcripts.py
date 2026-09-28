"""DbSessionWriter: the imp writer seam over state.db's transcripts table.

Duck-types ``imp.adapters.session.SessionWriter`` — ``Context`` calls
``write(message)`` synchronously on the event loop, so this writer keeps its
own short-lived-style sync ``sqlite3`` connection (WAL, busy timeout): one
indexed INSERT per message costs the same microseconds as the v1 file
append. Like the imp writer, an OS/database failure disables persistence
with a stderr warning instead of breaking the turn.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from imp.entities import ConversationMessage

from .db import BUSY_TIMEOUT_MS


class DbSessionWriter:
    """Appends each conversation message to the transcripts table, one row
    per message. Persistence only — the agent always runs from the in-memory
    Context."""

    def __init__(self, db_path: Path) -> None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.session_id = f"{stamp}-{secrets.token_hex(2)}"
        self.name = self.session_id  # /status and jobs_meta display this
        self.db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._seq = 0

    def __enter__(self) -> Self:
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                self.db_path,
                timeout=BUSY_TIMEOUT_MS / 1000,
                check_same_thread=False,
            )
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS transcripts ("
                "session_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL, "
                "message TEXT NOT NULL, PRIMARY KEY (session_id, seq))"
            )
            conn.commit()
            self._conn = conn
        except (OSError, sqlite3.Error) as exc:
            self._disable(exc)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def write(self, message: ConversationMessage) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "INSERT INTO transcripts(session_id, seq, ts, message) "
                "VALUES (?, ?, ?, ?)",
                (
                    self.session_id,
                    self._seq,
                    datetime.now(UTC).isoformat(),
                    json.dumps(
                        message.serialize(), ensure_ascii=False, default=str
                    ),
                ),
            )
            self._seq += 1
            self._conn.commit()
        except sqlite3.Error as exc:
            self._disable(exc)

    def _disable(self, exc: Exception) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        print(
            f"warning: session persistence disabled: {exc}", file=sys.stderr
        )
