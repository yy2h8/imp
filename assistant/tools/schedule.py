"""Scheduler tools: the model-facing way to add and cancel jobs.

The only writers of jobs/ from the model's side; storage and schema live in
assistant/jobstore.py. All validation failures return ToolResult error text the
model can act on — never exceptions.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from imp.tools.base import Tool, ToolResult

from .. import jobstore

SCHEDULE_HINT = (
    "Exactly one of at / at_local / every is required. "
    "at: ISO-8601 with UTC offset (2026-09-24T08:00:00+06:00). "
    "at_local: naive wall time 'YYYY-MM-DD HH:MM' in the owner's timezone. "
    "every: recurring interval in seconds. "
    "Jobs run in a fresh context: put everything the job needs into prompt."
)


class ScheduleJob(Tool):
    name = "schedule_job"
    description = "Schedule an agent run: one-shot at a time or recurring."
    instructions = SCHEDULE_HINT
    mutating = True  # serializes within a batch: jobs/ has no locking
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
            "description": "One-shot naive local time, 'YYYY-MM-DD HH:MM', "
            "resolved in the owner's timezone (IMP_TZ).",
        },
        "every": {
            "type": "integer",
            "description": "Recurring interval in seconds (> 0).",
        },
        "id": {
            "type": "string",
            "description": "Optional slug id ([a-z0-9-], max 64 chars). An "
            "existing id is replaced wholesale; omit to auto-generate.",
        },
    }
    required: ClassVar[list[str]] = ["prompt"]

    def __init__(self, tz: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.tz = tz

    async def execute(
        self,
        prompt: str,
        at: str | None = None,
        at_local: str | None = None,
        every: int | None = None,
        id: str | None = None,
    ) -> ToolResult:
        if not str(prompt).strip():
            return ToolResult(ok=False, content="prompt must not be empty")
        given = [
            name
            for name, value in (("at", at), ("at_local", at_local), ("every", every))
            if value is not None
        ]
        if len(given) != 1:
            return ToolResult(
                ok=False,
                content=f"exactly one of at/at_local/every is required, got {given or 'none'}. {SCHEDULE_HINT}",
            )
        home = self._home()
        if home is None:
            return ToolResult(ok=False, content="schedule_job has no workspace")
        job_id = await self._resolve_id(home, id)
        if isinstance(job_id, ToolResult):
            return job_id
        try:
            when: datetime | None = None
            every_int: int | None = None
            if given[0] == "at":
                when = datetime.fromisoformat(str(at).strip())
                if when.tzinfo is None:
                    return ToolResult(
                        ok=False,
                        content=f"at must carry a UTC offset (e.g. +00:00); got {at!r}",
                    )
            elif given[0] == "at_local":
                when = jobstore.resolve_local_time(str(at_local), self.tz)
            else:
                every_int = every
                if type(every_int) is not int or every_int <= 0:
                    return ToolResult(
                        ok=False,
                        content=f"every must be a positive integer, got {every!r}",
                    )
        except ValueError as exc:
            return ToolResult(ok=False, content=f"invalid schedule: {exc}")

        job = jobstore.Job(
            id=job_id,
            prompt=str(prompt).strip(),
            at=when.astimezone(UTC) if when is not None else None,
            every=every_int,
        )
        replaced = job_id in {
            j.id for j in (await asyncio.to_thread(jobstore.load_jobs, home))
        }
        await asyncio.to_thread(jobstore.save_job, home, job)
        verb = "replaced" if replaced else "scheduled"
        detail = (
            f"every {every_int}s" if every_int is not None else f"at {when.isoformat()}"
        )
        return ToolResult(
            ok=True,
            content=f"{verb} job {job_id!r} ({detail}); the owner will get the result as a message.",
        )

    async def _resolve_id(self, home, id: str | None):
        """Explicit ids validate against the slug rule; generated ids never
        replace an existing job (they get -2/-3... suffixes instead)."""
        if id is not None:
            job_id = str(id).strip()
            if not jobstore.valid_id(job_id):
                return ToolResult(
                    ok=False,
                    content=f"invalid id {job_id!r}: use lowercase letters, digits, hyphens (max 64)",
                )
            return job_id
        base = jobstore.generate_id(datetime.now(UTC))
        return jobstore.unique_id(
            {j.id for j in (await asyncio.to_thread(jobstore.load_jobs, home))}, base
        )

    def _home(self) -> Path | None:
        if self.config is None:
            return None
        return Path(self.config.workspace)


class UnscheduleJob(Tool):
    name = "unschedule_job"
    description = "Cancel a scheduled job by id; the job file is kept as a record."
    instructions = (
        "Cancelling sets status=cancelled and clears next_run; the file stays in "
        "jobs/ as a record. List jobs with list_dir/read_file on jobs/."
    )
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "id": {"type": "string", "description": "The job id to cancel."}
    }
    required: ClassVar[list[str]] = ["id"]

    async def execute(self, id: str) -> ToolResult:
        home = self._home()
        if home is None:
            return ToolResult(ok=False, content="unschedule_job has no workspace")
        for job in await asyncio.to_thread(jobstore.load_jobs, home):
            if job.id == id:
                revision = job.revision
                job.status = "cancelled"
                job.next_run = None
                if not await asyncio.to_thread(
                    jobstore.save_if_current, home, job, revision
                ):
                    return ToolResult(
                        ok=False, content="Job changed; retry cancellation."
                    )
                return ToolResult(ok=True, content=f"cancelled job {id!r}")
        return ToolResult(
            ok=False,
            content=f"no job with id {id!r}; list jobs/ to see current ids.",
        )

    def _home(self) -> Path | None:
        if self.config is None:
            return None
        return Path(self.config.workspace)
