"""The job store: pure data and functions over jobs/*.json (stdlib only).

Split from scheduler.py so the schedule_job/unschedule_job tools can share the
exact schema and validation without importing the app (app imports the tools —
a cycle otherwise). Nothing here does I/O beyond the jobs directory; storage
stays UTC, local time exists only at the tool boundary via resolve_local_time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from imp.adapters import FileSystemAdapter

JOB_STATUSES = ("pending", "running", "done", "error", "cancelled")
_STORE_LOCK = (
    threading.RLock()
)  # ponytail: one process/home; database if multiple writers are required
_LOG = logging.getLogger(__name__)
_ID_RE = re.compile(r"[a-z0-9-]{1,64}")


def _parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    ts = datetime.fromisoformat(str(value))
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)  # naive job timestamps mean UTC
    return ts.astimezone(UTC)


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


@dataclass(slots=True)
class Job:
    """One scheduled work item; ``at`` one-shots it, ``every`` repeats it."""

    id: str
    prompt: str
    at: datetime | None = None
    every: int | None = None
    last_run: datetime | None = None
    next_run: datetime | None = None
    status: str = "pending"
    revision: str = ""
    result: str = ""
    delivery_error: str = ""
    transcript: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        if not isinstance(data, dict):
            raise ValueError("Job must be a JSON object")  # noqa: TRY004
        every = data.get("every")
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Job prompt must be nonempty text")
        if (data.get("at") is not None) == (every is not None):
            raise ValueError("Job requires exactly one of at or every")
        status = str(data.get("status") or "pending")
        if status not in JOB_STATUSES:
            raise ValueError(f"unknown status: {status}")
        if every is not None and (type(every) is not int or every <= 0):
            raise ValueError(f"every must be positive: {every}")
        job_id = str(data.get("id") or "").strip()
        if not _ID_RE.fullmatch(job_id):
            raise ValueError(f"invalid job id: {job_id!r}")
        return cls(
            id=job_id,
            prompt=str(data.get("prompt") or ""),
            at=_parse_ts(data.get("at")),
            every=int(every) if every is not None else None,
            last_run=_parse_ts(data.get("last_run")),
            next_run=_parse_ts(data.get("next_run")),
            status=status,
            revision=str(data.get("revision") or ""),
            result=str(data.get("result") or ""),
            delivery_error=str(data.get("delivery_error") or ""),
            transcript=str(data.get("transcript") or ""),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "at": _iso(self.at),
            "every": self.every,
            "last_run": _iso(self.last_run),
            "next_run": _iso(self.next_run),
            "status": self.status,
            "revision": self.revision,
            "result": self.result,
            "delivery_error": self.delivery_error,
            "transcript": self.transcript,
        }


def valid_id(job_id: str) -> bool:
    return bool(_ID_RE.fullmatch(job_id))


def load_jobs(home: Path) -> list[Job]:
    with _STORE_LOCK:
        return _load_jobs(home)


def _load_jobs(home: Path) -> list[Job]:
    """Parse jobs/*.json; malformed or unsafely-named files are skipped."""
    jobs: list[Job] = []
    try:
        paths = sorted(FileSystemAdapter(home).resolve_path("jobs").glob("*.json"))
    except (OSError, ValueError):
        return []
    for path in paths:
        try:
            if path.is_symlink():
                raise ValueError("Job symlinks are unsupported")
            job = Job.from_dict(json.loads(FileSystemAdapter(home).read_bytes(path)))
        except (OSError, ValueError, TypeError):
            _LOG.warning("Skipping invalid job file %s", path.name)
            continue
        # strict structure: the file name is the id (save_job guarantees it)
        if job.prompt and job.id == path.stem:
            jobs.append(job)
    return jobs


def save_job(home: Path, job: Job) -> None:
    with _STORE_LOCK:
        if not valid_id(job.id):
            raise ValueError("Invalid job id")
        lexical = home / "jobs" / f"{job.id}.json"
        if lexical.is_symlink():
            raise ValueError("Job symlinks are unsupported")
        path = FileSystemAdapter(home).resolve_path(lexical)
        path.parent.mkdir(parents=True, exist_ok=True)
        if job.status == "pending" and job.every and job.next_run is None:
            job.next_run = (job.last_run or datetime.now(UTC)) + timedelta(
                seconds=job.every
            )
        job.revision = secrets.token_hex(16)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as fh:
            tmp = Path(fh.name)
            try:
                fh.write(json.dumps(job.to_dict(), indent=2) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save_if_current(home: Path, job: Job, revision: str) -> bool:
    with _STORE_LOCK:
        current = next((item for item in load_jobs(home) if item.id == job.id), None)
        if current is None or current.revision != revision:
            return False
        save_job(home, job)
        return True


def compute_next(job: Job, now: datetime) -> datetime | None:
    """When ``job`` should run next, or None (datetime/timedelta only).

    An explicit ``next_run`` wins; a never-run ``at`` is the first fire time;
    ``every`` repeats from the last run (or from now on first sight).
    """
    if job.status != "pending":
        return None
    if job.next_run is not None:
        return job.next_run
    if job.at is not None and job.last_run is None:
        return job.at
    if job.every is not None:
        return (job.last_run or now) + timedelta(seconds=job.every)
    return None


def next_due(jobs: list[Job], now: datetime) -> tuple[Job, datetime] | None:
    """The pending job with the earliest next fire time, if any."""
    best: tuple[Job, datetime] | None = None
    for job in jobs:
        due = compute_next(job, now)
        if due is not None and (best is None or due < best[1]):
            best = (job, due)
    return best


def advance(job: Job, ran_at: datetime) -> None:
    """Record a successful run: repeat or finish."""
    job.last_run = ran_at
    if job.every is not None:
        job.next_run = ran_at + timedelta(seconds=job.every)
        job.status = "pending"
    else:
        job.next_run = None
        job.status = "done"


def resolve_local_time(value: str, tz_name: str) -> datetime:
    """Local wall time in ``tz_name`` → aware UTC datetime.

    Accepts naive local input ("YYYY-MM-DD HH:MM", full ISO, or with seconds);
    an input that already carries an offset is converted, not re-interpreted.
    """
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
    """A fresh unique-by-construction id: job-20260923-090558 (UTC).
    Lowercase-only so generated ids satisfy the [a-z0-9-] slug rule."""
    return f"job-{now.astimezone(UTC):%Y%m%d-%H%M%S}"


def unique_id(existing: set[str], base: str) -> str:
    """First free variant of ``base``: base, base-2, base-3, …"""
    candidate, n = base, 1
    while candidate in existing:
        n += 1
        candidate = f"{base}-{n}"
    return candidate
