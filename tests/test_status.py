"""Status summary: local time, nearest jobs, context estimate, host, resilience."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from assistant.db import jobs_meta_upsert, open_db
from assistant.status import (
    NO_JOBS,
    UNAVAILABLE,
    _jobs_lines,
    collect_status,
    format_tokens,
    host_summary,
)

TZ = ZoneInfo("Asia/Almaty")
BASE = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def fake_app(tmp_path, **overrides):
    app = SimpleNamespace(
        assistant=SimpleNamespace(tz="Asia/Almaty", home=tmp_path),
        config=SimpleNamespace(api_key="sk-test-secret"),
        usage=(12_345, 128_000),
        scheduler=None,
        db=None,
    )
    for name, value in overrides.items():
        setattr(app, name, value)
    return app


class FakeScheduler:
    def __init__(self, jobs):
        self.jobs = jobs

    def get_jobs(self):
        return self.jobs


def job(job_id: str, when: datetime | None) -> SimpleNamespace:
    return SimpleNamespace(id=job_id, next_run_time=when)


async def test_collect_status_shows_all_sections(tmp_path):
    summary = await collect_status(fake_app(tmp_path))
    assert "Статус" in summary
    assert "Asia/Almaty" in summary
    assert "Задания" in summary
    assert "Контекст" in summary
    assert "Хост" in summary
    assert "OpenRouter" in summary
    assert UNAVAILABLE in summary  # scheduler/http absent on the fake app


async def test_collect_status_formats_fixed_now_in_local_tz(tmp_path):
    summary = await collect_status(
        fake_app(tmp_path), now=datetime(2026, 9, 30, 14, 32, tzinfo=TZ)
    )
    assert "🕐 30.09.2026 14:32 (Asia/Almaty)" in summary


async def test_collect_status_marks_context_as_estimate(tmp_path):
    summary = await collect_status(fake_app(tmp_path))
    assert "~12 345 / 128 000" in summary
    assert "(оценка)" in summary


def test_format_tokens_marks_estimate():
    assert format_tokens(12_345, 128_000) == "~12 345 / 128 000 токенов (оценка)"


def test_host_summary_reports_load_ram_disk(tmp_path):
    line = host_summary(tmp_path)
    assert "load" in line
    assert "RAM" in line
    assert "диск" in line


async def test_jobs_lines_sorted_limited_labeled(tmp_path):
    db = await open_db(tmp_path / "state.db")
    try:
        for i in range(5):
            await jobs_meta_upsert(
                db,
                schedule_id=f"job-{i}",
                label=f"описание {i}",
                prompt="p",
                tz="UTC",
                state="scheduled",
            )
        jobs = [job(f"job-{i}", BASE + timedelta(hours=i)) for i in range(5)]
        lines = await _jobs_lines(FakeScheduler(jobs), db, TZ)
        assert len(lines) == 3
        first = BASE.astimezone(TZ)
        assert lines[0] == f"• описание 0 — {first:%d.%m %H:%M}"
        second = (BASE + timedelta(hours=1)).astimezone(TZ)
        assert lines[1] == f"• описание 1 — {second:%d.%m %H:%M}"
    finally:
        await db.close()


async def test_jobs_lines_skips_paused_truncates_and_falls_back_to_id(tmp_path):
    db = await open_db(tmp_path / "state.db")
    try:
        await jobs_meta_upsert(
            db,
            schedule_id="known",
            label="д" * 50,
            prompt="p",
            tz="UTC",
            state="scheduled",
        )
        jobs = [
            job("unknown-id", BASE),
            job("known", BASE + timedelta(hours=1)),
            job("paused", None),
        ]
        lines = await _jobs_lines(FakeScheduler(jobs), db, TZ)
        assert len(lines) == 2
        assert lines[0].startswith("• unknown-id — ")
        assert "д" * 40 + "…" in lines[1]
    finally:
        await db.close()


async def test_jobs_lines_without_scheduler_is_unavailable():
    assert await _jobs_lines(None, None, TZ) == [UNAVAILABLE]


async def test_jobs_lines_empty_scheduler_reports_no_jobs():
    assert await _jobs_lines(FakeScheduler([]), None, TZ) == [NO_JOBS]


async def test_jobs_lines_without_db_shows_ids():
    lines = await _jobs_lines(FakeScheduler([job("job-0", BASE)]), None, TZ)
    local = BASE.astimezone(TZ)
    assert lines == [f"• job-0 — {local:%d.%m %H:%M}"]
