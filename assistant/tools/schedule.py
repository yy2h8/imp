"""Scheduler tools: the model-facing way to add and cancel durable jobs."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from imp.tools.base import Tool, ToolResult

from ..db import jobs_meta_get, jobs_meta_list, jobs_meta_update, jobs_meta_upsert
from ..scheduler import run_scheduled_job

_ID_RE = re.compile(r"[a-z0-9-]{1,64}")

SCHEDULE_HINT = (
    "Exactly one of at / at_local / every / cron is required. "
    "at: ISO-8601 with UTC offset (2026-09-24T08:00:00+06:00). "
    "at_local: naive wall time 'YYYY-MM-DD HH:MM' in the owner's timezone. "
    "every: recurring interval in seconds. "
    "cron: five-field cron expression in the owner's timezone. "
    "Jobs run in a fresh context: put everything the job needs into prompt."
)


def valid_id(job_id: str) -> bool:
    return bool(_ID_RE.fullmatch(job_id))


def resolve_local_time(value: str, tz_name: str) -> datetime:
    """Local wall time → aware UTC datetime."""
    try:
        parsed = datetime.fromisoformat(value.strip())
        zone = ZoneInfo(tz_name)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError(
            f"cannot resolve local time {value!r} in timezone {tz_name!r}: {exc}"
        ) from exc
    local = parsed.replace(tzinfo=zone) if parsed.tzinfo is None else parsed
    return local.astimezone(UTC)


def generate_id(now: datetime) -> str:
    return f"job-{now.astimezone(UTC):%Y%m%d-%H%M%S}"


def unique_id(existing: set[str], base: str) -> str:
    candidate, n = base, 1
    while candidate in existing:
        n += 1
        candidate = f"{base}-{n}"
    return candidate


class ScheduleJob(Tool):
    name = "schedule_job"
    description = "Schedule an agent run: one-shot, interval, or cron."
    instructions = SCHEDULE_HINT
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "prompt": {
            "type": "string",
            "description": "Self-contained instructions for the scheduled run.",
        },
        "at": {
            "type": "string",
            "description": "One-shot ISO-8601 timestamp with UTC offset.",
        },
        "at_local": {
            "type": "string",
            "description": "One-shot local wall time in the owner's timezone.",
        },
        "every": {
            "type": "integer",
            "description": "Recurring interval in seconds (> 0).",
        },
        "cron": {
            "type": "string",
            "description": "Recurring five-field cron expression in the owner's timezone.",
        },
        "id": {
            "type": "string",
            "description": "Optional slug id ([a-z0-9-], max 64 chars).",
        },
    }
    required: ClassVar[list[str]] = ["prompt"]

    def __init__(
        self,
        tz: str,
        scheduler: AsyncIOScheduler | None = None,
        db: aiosqlite.Connection | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.tz = tz
        self.scheduler = scheduler
        self.db = db

    async def execute(
        self,
        prompt: str,
        at: str | None = None,
        at_local: str | None = None,
        every: int | None = None,
        cron: str | None = None,
        id: str | None = None,
    ) -> ToolResult:
        if not prompt.strip():
            return ToolResult(ok=False, content="prompt must not be empty")
        if self.scheduler is None or self.db is None:
            return ToolResult(ok=False, content="scheduler is unavailable")
        given = [
            name
            for name, value in (
                ("at", at), ("at_local", at_local), ("every", every), ("cron", cron)
            )
            if value is not None
        ]
        if len(given) != 1:
            return ToolResult(
                ok=False,
                content=f"exactly one of at/at_local/every/cron is required, got {given or 'none'}. {SCHEDULE_HINT}",
            )
        try:
            if given[0] == "at":
                when = datetime.fromisoformat(str(at).strip())
                if when.tzinfo is None:
                    return ToolResult(
                        ok=False,
                        content=f"at must carry a UTC offset (e.g. +00:00); got {at!r}",
                    )
                trigger = DateTrigger(run_date=when.astimezone(UTC))
                detail = f"at {when.astimezone(UTC).isoformat()}"
            elif given[0] == "at_local":
                when = resolve_local_time(str(at_local), self.tz)
                trigger = DateTrigger(run_date=when)
                detail = f"at {when.isoformat()}"
            elif given[0] == "every":
                if type(every) is not int or every <= 0:
                    return ToolResult(
                        ok=False, content=f"every must be a positive integer, got {every!r}"
                    )
                trigger = IntervalTrigger(seconds=every)
                detail = f"every {every}s"
            else:
                trigger = CronTrigger.from_crontab(str(cron), timezone=ZoneInfo(self.tz))
                detail = f"cron {cron!r} ({self.tz})"
        except (ValueError, ZoneInfoNotFoundError) as exc:
            return ToolResult(ok=False, content=f"invalid schedule: {exc}")

        job_id = await self._resolve_id(id)
        if isinstance(job_id, ToolResult):
            return job_id
        replaced = self.scheduler.get_job(job_id) is not None
        self.scheduler.add_job(
            run_scheduled_job,
            trigger=trigger,
            args=[job_id, prompt.strip()],
            id=job_id,
            replace_existing=True,
        )
        await jobs_meta_upsert(
            self.db,
            schedule_id=job_id,
            label=prompt.strip()[:80],
            prompt=prompt.strip(),
            tz=self.tz,
            state="scheduled",
        )
        verb = "replaced" if replaced else "scheduled"
        return ToolResult(
            ok=True,
            content=f"{verb} job {job_id!r} ({detail}); the owner will get the result as a message.",
        )

    async def _resolve_id(self, requested: str | None) -> str | ToolResult:
        if requested is not None:
            job_id = str(requested).strip()
            if not valid_id(job_id):
                return ToolResult(
                    ok=False,
                    content=f"invalid id {job_id!r}: use lowercase letters, digits, hyphens (max 64)",
                )
            return job_id
        assert self.scheduler is not None and self.db is not None
        existing = {job.id for job in self.scheduler.get_jobs()}
        existing.update(row["schedule_id"] for row in await jobs_meta_list(self.db))
        return unique_id(existing, generate_id(datetime.now(UTC)))


class UnscheduleJob(Tool):
    name = "unschedule_job"
    description = "Cancel a scheduled job by id; its result record is kept."
    instructions = "Use list_jobs to see current ids; cancelled jobs remain in history."
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "id": {"type": "string", "description": "The job id to cancel."}
    }
    required: ClassVar[list[str]] = ["id"]

    def __init__(
        self,
        scheduler: AsyncIOScheduler | None = None,
        db: aiosqlite.Connection | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.scheduler = scheduler
        self.db = db

    async def execute(self, id: str) -> ToolResult:
        if self.scheduler is None or self.db is None:
            return ToolResult(ok=False, content="scheduler is unavailable")
        try:
            self.scheduler.remove_job(id)
        except JobLookupError:
            current = ", ".join(job.id for job in self.scheduler.get_jobs()) or "none"
            return ToolResult(
                ok=False, content=f"no job with id {id!r}; current ids: {current}."
            )
        row = await jobs_meta_get(self.db, id)
        if row is not None:
            await jobs_meta_update(self.db, id, state="cancelled")
        return ToolResult(ok=True, content=f"cancelled job {id!r}")
