"""schedule_job / unschedule_job over APScheduler + jobs_meta (+ cron)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.jobstores.base import JobLookupError
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from assistant.db import STATE_DB_NAME, jobs_meta_get, open_db
from assistant.tools.schedule import ScheduleJob, UnscheduleJob
from imp.config import Config

_OPEN_CONNECTIONS = []


@pytest.fixture(autouse=True)
async def close_test_databases():
    yield
    while _OPEN_CONNECTIONS:
        await _OPEN_CONNECTIONS.pop().close()


class FakeScheduler:
    def __init__(self) -> None:
        self.jobs: dict[str, tuple] = {}

    def add_job(self, func, trigger=None, args=None, id=None, **kwargs):
        self.jobs[id] = (func, trigger, tuple(args or ()), kwargs)
        return SimpleNamespace(id=id)

    def get_jobs(self):
        return [SimpleNamespace(id=job_id) for job_id in self.jobs]

    def get_job(self, job_id):
        return SimpleNamespace(id=job_id) if job_id in self.jobs else None

    def remove_job(self, job_id):
        if job_id not in self.jobs:
            raise JobLookupError(job_id)
        del self.jobs[job_id]


async def make_db(tmp_path: Path):
    conn = await open_db(tmp_path / STATE_DB_NAME)
    _OPEN_CONNECTIONS.append(conn)
    return conn


def make_tool(tmp_path: Path, scheduler: FakeScheduler, conn, tz="Asia/Almaty"):
    return ScheduleJob(
        config=Config(api_key="k", workspace=tmp_path),
        tz=tz,
        scheduler=scheduler,
        db=conn,
    )


def make_unschedule(tmp_path: Path, scheduler: FakeScheduler, conn):
    return UnscheduleJob(
        config=Config(api_key="k", workspace=tmp_path),
        scheduler=scheduler,
        db=conn,
    )


class TestScheduleJob:
    async def test_at_local_resolves_via_tz(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        result = await make_tool(tmp_path, scheduler, conn).execute(
            prompt="digest", at_local="2026-09-24 08:00"
        )
        assert result.ok
        (job_id, prompt) = next(iter(scheduler.jobs.values()))[2]
        assert prompt == "digest"
        trigger = next(iter(scheduler.jobs.values()))[1]
        assert isinstance(trigger, DateTrigger)
        assert trigger.run_date == datetime(2026, 9, 24, 3, 0, tzinfo=UTC)
        row = await jobs_meta_get(conn, job_id)
        assert row["state"] == "scheduled" and row["tz"] == "Asia/Almaty"

    async def test_at_with_offset_is_kept_in_utc(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        result = await make_tool(tmp_path, scheduler, conn).execute(
            prompt="x", at="2026-09-24T08:00:00+06:00"
        )
        assert result.ok
        (_, trigger, _, _) = next(iter(scheduler.jobs.values()))
        assert isinstance(trigger, DateTrigger)
        assert trigger.run_date == datetime(2026, 9, 24, 2, 0, tzinfo=UTC)

    async def test_every_uses_interval(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        result = await make_tool(tmp_path, scheduler, conn).execute(
            prompt="tick", every=300
        )
        assert result.ok
        (_, trigger, _, _) = next(iter(scheduler.jobs.values()))
        assert isinstance(trigger, IntervalTrigger)
        assert trigger.interval.total_seconds() == 300

    async def test_cron_in_owner_tz(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        result = await make_tool(tmp_path, scheduler, conn).execute(
            prompt="daily report", cron="0 9 * * *"
        )
        assert result.ok
        (_, trigger, _, _) = next(iter(scheduler.jobs.values()))
        assert isinstance(trigger, CronTrigger)
        assert "hour='9'" in str(trigger)

    async def test_exactly_one_schedule_required(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        tool = make_tool(tmp_path, scheduler, conn)
        none_given = await tool.execute(prompt="x")
        assert not none_given.ok and "exactly one" in none_given.content
        two_given = await tool.execute(prompt="x", every=60, cron="0 9 * * *")
        assert not two_given.ok and "exactly one" in two_given.content
        assert scheduler.jobs == {}

    async def test_invalid_cron_is_tool_error(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        result = await make_tool(tmp_path, scheduler, conn).execute(
            prompt="x", cron="not a cron"
        )
        assert not result.ok
        assert "invalid schedule" in result.content.lower()

    async def test_generated_ids_never_collide_explicit_replaces(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        tool = make_tool(tmp_path, scheduler, conn)
        first = await tool.execute(prompt="a", every=60)
        assert first.ok
        second = await tool.execute(prompt="b", every=60)  # next second id
        assert second.ok
        assert len(scheduler.jobs) == 2
        replaced = await tool.execute(prompt="c", every=60, id="daily")
        assert replaced.ok and "replaced" not in replaced.content
        again = await tool.execute(prompt="d", every=90, id="daily")
        assert again.ok and "replaced" in again.content
        assert len(scheduler.jobs) == 3
        row = await jobs_meta_get(conn, "daily")
        assert row["prompt"] == "d"

    async def test_invalid_id_rejected(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        result = await make_tool(tmp_path, scheduler, conn).execute(
            prompt="x", every=60, id="Bad ID!"
        )
        assert not result.ok and "id" in result.content.lower()


class TestUnscheduleJob:
    async def test_cancels_and_marks_cancelled(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        await make_tool(tmp_path, scheduler, conn).execute(prompt="x", every=60, id="daily")
        result = await make_unschedule(tmp_path, scheduler, conn).execute(id="daily")
        assert result.ok
        assert "daily" not in scheduler.jobs
        row = await jobs_meta_get(conn, "daily")
        assert row["state"] == "cancelled"

    async def test_missing_id_lists_current(self, tmp_path):
        scheduler, conn = FakeScheduler(), await make_db(tmp_path)
        await make_tool(tmp_path, scheduler, conn).execute(prompt="x", every=60, id="daily")
        result = await make_unschedule(tmp_path, scheduler, conn).execute(id="nope")
        assert not result.ok
        assert "daily" in result.content

    async def test_no_scheduler_is_tool_error(self, tmp_path):
        tool = ScheduleJob(
            config=Config(api_key="k", workspace=tmp_path), tz="UTC"
        )
        result = await tool.execute(prompt="x", every=60)
        assert not result.ok
