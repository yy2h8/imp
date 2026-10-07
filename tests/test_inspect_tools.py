"""Agent inspection tools over jobs, transcripts, turn costs and queue."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from assistant.db import (
    STATE_DB_NAME,
    jobs_meta_upsert,
    open_db,
    queue_push,
    turn_insert,
)
from assistant.tools.inspect import CostReport, ListJobs, QueueStatus, SearchTranscripts
from imp.config import Config


@pytest.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / STATE_DB_NAME)
    yield conn
    await conn.close()


class FakeScheduler:
    def __init__(self, *jobs):
        self.jobs = list(jobs)

    def get_jobs(self):
        return self.jobs


async def test_list_jobs_joins_next_fire_with_metadata(db, tmp_path):
    next_run = datetime(2026, 10, 1, 9, tzinfo=UTC)
    await jobs_meta_upsert(
        db, schedule_id="daily", label="Daily report", prompt="p", tz="UTC",
        state="scheduled", result="last result",
    )
    scheduler = FakeScheduler(
        SimpleNamespace(id="daily", next_run_time=next_run),
        SimpleNamespace(id="orphan", next_run_time=None),
    )
    result = await ListJobs(
        config=Config(api_key="k", workspace=tmp_path), db=db, scheduler=scheduler
    ).execute()
    assert result.ok
    assert "daily" in result.content and "Daily report" in result.content
    assert "2026-10-01" in result.content and "last result" in result.content
    assert "orphan" in result.content



async def test_list_jobs_appends_last_run_summary_from_transcript(db, tmp_path):
    await turn_insert(
        db,
        ts=datetime.now(UTC).isoformat(),
        kind="job",
        session_id="sess-9",
        model="m",
        in_tokens=10,
        out_tokens=5,
        cost_usd=0.02,
        tools=2,
        seconds=12,
        ok=True,
    )
    await jobs_meta_upsert(
        db,
        schedule_id="daily",
        label="Daily report",
        prompt="p",
        tz="UTC",
        state="scheduled",
        transcript="sess-9",
    )
    result = await ListJobs(
        config=Config(api_key="k", workspace=tmp_path), db=db, scheduler=None
    ).execute()
    assert result.ok
    assert "last run: ✓ готово · инструментов: 2 · 12 с · $0.0200" in result.content


async def test_list_jobs_without_transcript_has_no_last_run(db, tmp_path):
    await jobs_meta_upsert(
        db,
        schedule_id="one-shot",
        label="once",
        prompt="p",
        tz="UTC",
        state="done",
    )
    result = await ListJobs(
        config=Config(api_key="k", workspace=tmp_path), db=db, scheduler=None
    ).execute()
    assert result.ok
    assert "last run:" not in result.content


async def test_search_transcripts_finds_and_misses(db, tmp_path):
    await db.execute(
        "INSERT INTO transcripts(session_id, seq, ts, message) VALUES (?, ?, ?, ?)",
        ("s1", 0, "2026-01-01T00:00:00+00:00", json.dumps({"role": "user", "content": "armbian box"})),
    )
    await db.commit()
    tool = SearchTranscripts(config=Config(api_key="k", workspace=tmp_path), db=db)
    found = await tool.execute(query="armbian")
    assert found.ok and "s1" in found.content and "armbian box" in found.content
    missing = await tool.execute(query="absent")
    assert missing.ok and "No transcript matches" in missing.content


async def test_cost_report_aggregates_period(db, tmp_path):
    await turn_insert(
        db, ts=datetime.now(UTC).isoformat(), kind="interactive", session_id="s",
        model="m", in_tokens=45000, out_tokens=8000, cost_usd=0.42,
        tools=3, seconds=10, ok=True,
    )
    result = await CostReport(
        config=Config(api_key="k", workspace=tmp_path), db=db
    ).execute(period="day")
    assert result.ok
    assert "turns 1" in result.content
    assert "in 45k" in result.content and "out 8k" in result.content
    assert "$0.4200" in result.content


async def test_queue_status_reports_waiting_depth_and_active_turn(db, tmp_path):
    await queue_push(db, "text", "one")
    await queue_push(db, "text", "two")
    result = await QueueStatus(
        config=Config(api_key="k", workspace=tmp_path), db=db,
        is_turn_active=lambda: True,
    ).execute()
    assert result.ok and "2 waiting" in result.content and "turn active" in result.content
