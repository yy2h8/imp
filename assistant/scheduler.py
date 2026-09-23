"""Internal asyncio scheduler over jobs/*.json (D8: no host cron, stdlib only).

A single task sleeps until the earliest due job, runs it in a fresh context
(a job never inherits or pollutes the interactive chat), delivers the result
to the owner, and updates the job file. Jobs are re-read every cycle, so they
survive restarts; a failing job is marked ``error`` and never stops the loop.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from imp.agent import Agent, EventType

from .app import AssistantApp, Session

IDLE_POLL_S = 30.0  # re-read jobs/ this often while nothing is due
MAX_SLEEP_S = 3600.0  # re-check at least hourly (clock drift, edited jobs)


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

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        every = data.get("every")
        return cls(
            id=str(data.get("id") or "").strip(),
            prompt=str(data.get("prompt") or ""),
            at=_parse_ts(data.get("at")),
            every=int(every) if every is not None else None,
            last_run=_parse_ts(data.get("last_run")),
            next_run=_parse_ts(data.get("next_run")),
            status=str(data.get("status") or "pending"),
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
        }


def load_jobs(home: Path) -> list[Job]:
    """Parse jobs/*.json; malformed or unsafely-named files are skipped."""
    jobs: list[Job] = []
    try:
        paths = sorted((home / "jobs").glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            job = Job.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            continue
        if job.id and job.prompt and job.id == Path(job.id).name:
            jobs.append(job)
    return jobs


def save_job(home: Path, job: Job) -> None:
    path = home / "jobs" / f"{job.id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(job.to_dict(), indent=2) + "\n", encoding="utf-8")


def compute_next(job: Job, now: datetime) -> datetime | None:
    """When ``job`` should run next, or None (D8: datetime/timedelta only).

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


class Scheduler:
    """The never-dying scheduler task; owns no resources beyond the app."""

    def __init__(self, app: AssistantApp) -> None:
        self.app = app

    @property
    def home(self) -> Path:
        return self.app.assistant.home

    async def run(self) -> None:
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(IDLE_POLL_S)  # §7: a failure never stops us

    async def _cycle(self) -> None:
        due = next_due(load_jobs(self.home), datetime.now(UTC))
        if due is None:
            await asyncio.sleep(IDLE_POLL_S)
            return
        job, at = due
        wait = (at - datetime.now(UTC)).total_seconds()
        if wait > 0:
            await asyncio.sleep(min(wait, MAX_SLEEP_S))
            return
        await self.run_job(job)

    async def run_job(self, job: Job) -> None:
        ran_at = datetime.now(UTC)
        try:
            answer = await self._execute(job)
        except Exception as exc:
            job.status = "error"
            job.last_run = ran_at
            job.next_run = None
            save_job(self.home, job)
            await self.app.bot.send_message(
                self.app.chat_id, f"Job {job.id} failed: {exc}"
            )
            return
        advance(job, ran_at)
        save_job(self.home, job)
        if answer:
            await self.app.bot.send_message(
                self.app.chat_id, f"⏰ {job.id}\n\n{answer}"
            )

    async def _execute(self, job: Job) -> str:
        """Run the job in a fresh context: second Session, same system prompt,
        shared tools/client — nothing leaks into or out of the chat."""
        system_prompt = self.app.session.context.messages[0].content
        session = Session.open(self.app.config, system_prompt)
        agent = Agent(
            config=self.app.config,
            tools=self.app.agent.tools,
            client=self.app.agent.client,
            context=session.context,
        )
        answer = ""
        try:
            async for event in agent.run_turn(job.prompt):
                if event.type is EventType.MODEL_RESPONSE and event.quote:
                    answer = event.quote
                elif event.type is EventType.ERROR and event.error_message:
                    raise RuntimeError(event.error_message)
        finally:
            session.writer.__exit__(None, None, None)
        return answer
