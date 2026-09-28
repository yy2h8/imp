"""DbSessionWriter: imp's writer seam backed by the transcripts table."""

from __future__ import annotations

import json
import sys

from imp.agent.context import Context
from imp.entities import AssistantMessage, TextMessage, ToolMessage

from assistant.db import STATE_DB_NAME, open_db, transcript_search
from assistant.transcripts import DbSessionWriter


async def test_writer_records_context_messages_in_order(config, tmp_path):
    db_path = tmp_path / STATE_DB_NAME
    conn = await open_db(db_path)  # schema exists before the sync writer runs
    await conn.close()
    writer = DbSessionWriter(db_path)
    writer.__enter__()
    try:
        context = Context(config=config, system_prompt="sys", writer=writer)
        context.append(TextMessage(role="user", content="find the armbian box"))
        context.append(AssistantMessage(content="on it"))
        context.append(ToolMessage(call_id="1", content="result"))
    finally:
        writer.__exit__(None, None, None)

    conn = await open_db(db_path)
    rows = await conn.execute_fetchall(
        "SELECT seq, message FROM transcripts WHERE session_id = ? "
        "ORDER BY seq",
        (writer.session_id,),
    )
    assert [row[0] for row in rows] == [0, 1, 2, 3]
    parsed = [json.loads(row[1]) for row in rows]
    assert parsed[0] == {"role": "system", "content": "sys"}
    assert parsed[1] == {"role": "user", "content": "find the armbian box"}
    hits = await transcript_search(conn, "armbian")
    assert hits and hits[0][0] == writer.session_id
    await conn.close()


def test_session_id_format_and_uniqueness(tmp_path):
    db_path = tmp_path / STATE_DB_NAME
    first = DbSessionWriter(db_path)
    second = DbSessionWriter(db_path)
    assert first.name == first.session_id
    # stamp-token format like the v1 file stems: 20260928T101500-ab12
    assert len(first.name) == len("20260928T101500-ab12")
    assert first.name != second.name


def test_erroring_db_disables_persistence_without_raising(tmp_path, capsys):
    db_path = tmp_path / "missing" / "nested" / STATE_DB_NAME  # unwritable
    writer = DbSessionWriter(db_path)
    writer.__enter__()  # mkdir fails → disabled, warning on stderr
    writer.write(TextMessage(role="user", content="hello"))
    writer.__exit__(None, None, None)  # no raise
    assert "session persistence disabled" in capsys.readouterr().err
