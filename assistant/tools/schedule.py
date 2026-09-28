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
    "Use one schedule object {mode, value}; never pass at, at_local, every, or cron separately. "
    "Choose exactly one mode. "
    "at: ISO-8601 with UTC offset; at_local: naive 'YYYY-MM-DD HH:MM' in the owner's timezone; "
    "every: positive interval seconds; cron: five-field expression in the owner's timezone. "
    "Jobs run in a fresh context: put everything the job needs into prompt."
)

_SCHEDULE_MODES = ("at", "at_local", "every", "cron")


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
        "schedule": {
            "type": "object",
            "description": SCHEDULE_HINT,
            "properties": {
                "mode": {"type": "string", "enum": list(_SCHEDULE_MODES)},
                "value": {
                    "type": "string",
                    "description": "Schedule value for the selected mode; interval seconds as digits.",
                },
            },
            "required": ["mode", "value"],
            "additionalProperties": False,
        },
        "id": {
            "type": "string",
            "description": "Optional slug id ([a-z0-9-], max 64 chars).",
        },
    }
    required: ClassVar[list[str]] = ["prompt", "schedule"]

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
        schedule: dict[str, Any] | None = None,
        id: str | None = None,
    ) -> ToolResult:
        if not prompt.strip():
            return ToolResult(ok=False, content="prompt must not be empty")
        if self.scheduler is None or self.db is None:
            return ToolResult(ok=False, content="scheduler is unavailable")
        if not isinstance(schedule, dict) or set(schedule) != {"mode", "value"}:
            return ToolResult(
                ok=False,
                content="schedule must contain only mode and value",
            )
        mode, value = schedule["mode"], schedule["value"]
        if mode not in _SCHEDULE_MODES:
            return ToolResult(
                ok=False,
                content=f"schedule mode must be one of {_SCHEDULE_MODES}, got {mode!r}",
            )
        try:
            if mode == "at":
                if not isinstance(value, str) or not value.strip():
                    return ToolResult(ok=False, content="at value must be a timestamp")
                when = datetime.fromisoformat(value.strip())
                if when.tzinfo is None:
                    return ToolResult(
                        ok=False,
                        content=f"at must carry a UTC offset (e.g. +00:00); got {value!r}",
                    )
                trigger = DateTrigger(run_date=when.astimezone(UTC))
                detail = f"at {when.astimezone(UTC).isoformat()}"
            elif mode == "at_local":
                if not isinstance(value, str) or not value.strip():
                    return ToolResult(ok=False, content="at_local value must be a local timestamp")
                when = resolve_local_time(value, self.tz)
                trigger = DateTrigger(run_date=when)
                detail = f"at {when.isoformat()}"
            elif mode == "every":
                if type(value) is int:
                    seconds = value
                elif isinstance(value, str):
                    try:
                        seconds = int(value.strip())
                    except ValueError:
                        return ToolResult(
                            ok=False,
                            content=f"every must be a positive integer, got {value!r}",
                        )
                else:
                    return ToolResult(
                        ok=False, content=f"every must be a positive integer, got {value!r}"
                    )
                if seconds <= 0:
                    return ToolResult(
                        ok=False, content=f"every must be a positive integer, got {value!r}"
                    )
                trigger = IntervalTrigger(seconds=seconds)
                detail = f"every {seconds}s"
            else:
                if not isinstance(value, str) or not value.strip():
                    return ToolResult(ok=False, content="cron value must not be empty")
                trigger = CronTrigger.from_crontab(value.strip(), timezone=ZoneInfo(self.tz))
                detail = f"cron {value!r} ({self.tz})"
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
