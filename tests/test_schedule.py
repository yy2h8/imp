"""schedule_job / unschedule_job tools: strict schema, atomic store, cancel."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from assistant.jobstore import load_jobs
from assistant.tools.schedule import ScheduleJob, UnscheduleJob
from imp.config import Config


def make_tool(tmp_path: Path, tz: str = "Asia/Almaty") -> ScheduleJob:
    return ScheduleJob(config=Config(api_key="k", workspace=tmp_path), tz=tz)


def make_unschedule(tmp_path: Path) -> UnscheduleJob:
    return UnscheduleJob(config=Config(api_key="k", workspace=tmp_path))


class TestScheduleJob:
    async def test_at_local_resolves_via_tz(self, tmp_path):
        result = await make_tool(tmp_path).execute(
            prompt="digest", at_local="2026-09-24 08:00"
        )
        assert result.ok
        (job,) = load_jobs(tmp_path)
        # Almaty is UTC+5 (since 2024): 08:00 local = 03:00 UTC
        assert job.at == datetime(2026, 9, 24, 3, 0, tzinfo=UTC)
        assert job.status == "pending" and job.every is None

    async def test_at_requires_offset(self, tmp_path):
        result = await make_tool(tmp_path).execute(prompt="x", at="2026-09-24T08:00:00")
        assert not result.ok
        assert "offset" in result.content
        assert load_jobs(tmp_path) == []

    async def test_at_with_offset_is_kept_in_utc(self, tmp_path):
        result = await make_tool(tmp_path).execute(
            prompt="x", at="2026-09-24T08:00:00+06:00"
        )
        assert result.ok
        (job,) = load_jobs(tmp_path)
        assert job.at == datetime(2026, 9, 24, 2, 0, tzinfo=UTC)

    async def test_every_recurring(self, tmp_path):
        result = await make_tool(tmp_path).execute(prompt="tick", every=300)
        assert result.ok
        (job,) = load_jobs(tmp_path)
        assert job.every == 300 and job.at is None

    async def test_every_must_be_positive(self, tmp_path):
        for bad in (0, -5):
            result = await make_tool(tmp_path).execute(prompt="x", every=bad)
            assert not result.ok
        assert load_jobs(tmp_path) == []

    async def test_exactly_one_schedule_required(self, tmp_path):
        tool = make_tool(tmp_path)
        none = await tool.execute(prompt="x")
        both = await tool.execute(prompt="x", at="2026-09-24T08:00:00+00:00", every=60)
        assert not none.ok and not both.ok
        assert load_jobs(tmp_path) == []

    async def test_empty_prompt_rejected(self, tmp_path):
        result = await make_tool(tmp_path).execute(prompt="  ", every=60)
        assert not result.ok

    async def test_explicit_id_validated(self, tmp_path):
        ok = await make_tool(tmp_path).execute(prompt="x", every=60, id="morning-news")
        assert ok.ok
        bad = await make_tool(tmp_path).execute(prompt="x", every=60, id="Bad_ID!")
        assert not bad.ok
        assert {j.id for j in load_jobs(tmp_path)} == {"morning-news"}

    async def test_same_id_replaces_wholesale(self, tmp_path):
        tool = make_tool(tmp_path)
        await tool.execute(prompt="v1", every=60, id="digest")
        # pretend it ran once: replace must clear the run state
        path = tmp_path / "jobs" / "digest.json"
        data = json.loads(path.read_text())
        data["last_run"] = "2026-09-23T00:00:00+00:00"
        data["status"] = "error"
        path.write_text(json.dumps(data))
        result = await tool.execute(
            prompt="v2", at="2026-09-25T08:00:00+00:00", id="digest"
        )
        assert result.ok and "replaced" in result.content
        (job,) = load_jobs(tmp_path)
        assert job.prompt == "v2" and job.status == "pending"
        assert job.last_run is None and job.every is None

    async def test_generated_id_never_replaces(self, tmp_path):
        tool = make_tool(tmp_path)
        first = await tool.execute(prompt="a", every=60)
        second = await tool.execute(prompt="b", every=60)
        assert first.ok and second.ok
        jobs = load_jobs(tmp_path)
        assert len(jobs) == 2
        assert {j.prompt for j in jobs} == {"a", "b"}
        for job in jobs:  # job-YYYYmmdd-HHMMSS, slug-safe
            assert job.id.startswith("job-")
            assert job.id == job.id.lower()

    async def test_at_local_without_valid_tz_errors(self, tmp_path):
        result = await make_tool(tmp_path, tz="Not/AZone").execute(
            prompt="x", at_local="2026-09-24 08:00"
        )
        assert not result.ok
        assert "cannot resolve local time" in result.content

    async def test_store_is_atomic_shape(self, tmp_path):
        await make_tool(tmp_path).execute(prompt="x", every=60, id="shaped")
        data = json.loads((tmp_path / "jobs" / "shaped.json").read_text())
        assert set(data) == {
            "id",
            "prompt",
            "at",
            "every",
            "last_run",
            "next_run",
            "status",
            "revision",
            "result",
            "delivery_error",
            "transcript",
        }
        assert not list((tmp_path / "jobs").glob("*.tmp"))


class TestUnscheduleJob:
    async def test_cancel_sets_status_keeps_file(self, tmp_path):
        await make_tool(tmp_path).execute(prompt="x", every=60, id="j1")
        result = await make_unschedule(tmp_path).execute(id="j1")
        assert result.ok
        (job,) = load_jobs(tmp_path)  # the file stays; compute_next ignores it
        assert job.id == "j1"

    async def test_missing_id_is_an_error(self, tmp_path):
        result = await make_unschedule(tmp_path).execute(id="nope")
        assert not result.ok
