"""Internal asyncio scheduler over jobs/*.json (no host cron, stdlib only).

A single task sleeps until the earliest due job, runs it in a fresh context
(a job never inherits or pollutes the interactive chat), delivers the result
to the owner, and updates the job file. Jobs are re-read every cycle, so they
survive restarts; a failing job is marked ``error`` and never stops the loop.

The Job dataclass, schema, and load/save live in jobstore.py — shared with the
schedule_job/unschedule_job tools, which are the only writers from the model's
side.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path

from imp.agent import Agent, EventType
from imp.tools.ask import Ask

from .adapters.telegram import send_text
from .app import AssistantApp, Session
from .jobstore import Job, advance, load_jobs, next_due, save_if_current

IDLE_POLL_S = 30.0  # re-read jobs/ this often while nothing is due

_LOG = logging.getLogger(__name__)


def _preview(text: str, limit: int = 60) -> str:
    line = " ".join(text.split())
    return line[:limit] + ("…" if len(line) > limit else "")


class Scheduler:
    """The never-dying scheduler task; owns no resources beyond the app."""

    def __init__(self, app: AssistantApp) -> None:
        self.app = app

    @property
    def home(self) -> Path:
        return self.app.assistant.home

    async def run(self) -> None:
        for job in await asyncio.to_thread(load_jobs, self.home):
            if job.status == "running":
                _LOG.info("job %s interrupted by shutdown; marked error", job.id)
                revision = job.revision
                job.status = "error"
                job.result = "Interrupted by shutdown; actions may already have occurred. Reschedule manually."
                job.next_run = None
                if await asyncio.to_thread(save_if_current, self.home, job, revision):
                    await self._deliver(job)
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOG.exception("Scheduler cycle failed")
                await asyncio.sleep(IDLE_POLL_S)  # a failure never stops us

    async def _cycle(self) -> None:
        jobs = await asyncio.to_thread(load_jobs, self.home)
        for job in jobs:
            if job.status == "pending" and job.every and job.next_run is None:
                await asyncio.to_thread(save_if_current, self.home, job, job.revision)
        due = next_due(jobs, datetime.now(UTC))
        if due is None:
            await asyncio.sleep(IDLE_POLL_S)
            return
        job, at = due
        wait = (at - datetime.now(UTC)).total_seconds()
        if wait > 0:
            await asyncio.sleep(min(wait, IDLE_POLL_S))
            return
        await self.run_job(job)

    async def run_job(self, job: Job) -> None:
        async with self.app.execution_lock:
            revision = job.revision
            job.status = "running"
            if not await asyncio.to_thread(save_if_current, self.home, job, revision):
                return
            revision = job.revision
            _LOG.info("job %s started: %s", job.id, _preview(job.prompt))
            try:
                job.result = await self._execute(job)
            except Exception as exc:
                _LOG.warning("job %s failed: %s", job.id, exc)
                job.status = "error"
                job.last_run = datetime.now(UTC)
                job.next_run = None
                job.result = f"Job {job.id} failed: {exc}"
            else:
                advance(job, datetime.now(UTC))
                _LOG.info("job %s finished; next run %s", job.id, job.next_run)
            if await asyncio.to_thread(save_if_current, self.home, job, revision):
                await self._deliver(job)

    async def _deliver(self, job: Job) -> None:
        revision = job.revision
        try:
            if job.result:
                await send_text(
                    self.app.bot, self.app.chat_id, f"⏰ {job.id}\n\n{job.result}"
                )
            job.delivery_error = ""
        except Exception as exc:
            job.delivery_error = str(exc)
            _LOG.error("Job %s delivery failed: %s", job.id, exc)
        await asyncio.to_thread(save_if_current, self.home, job, revision)

    async def _execute(self, job: Job) -> str:
        """Run in a fresh context with no access to interactive questions."""
        system_prompt = await asyncio.to_thread(self.app.build_prompt)
        system_prompt += (
            "\n\nThis is a non-interactive scheduled job. The ask tool is unavailable. "
            "Do not ask questions or wait for replies. If essential information "
            "is missing, report what prevented completion."
        )
        session = Session.open(self.app.config, system_prompt)
        job.transcript = session.writer.path.name
        agent = Agent(
            config=self.app.config,
            tools={
                name: tool
                for name, tool in self.app.agent.tools.items()
                if name != Ask.name
            },
            client=self.app.agent.client,
            context=session.context,
        )
        answer = ""
        try:
            async with aclosing(agent.run_turn(job.prompt)) as events:
                async for event in events:
                    if event.type is EventType.MODEL_RESPONSE:
                        answer = event.quote or ""
                    elif event.type is EventType.ERROR and event.error_message:
                        raise RuntimeError(event.error_message)
        finally:
            session.writer.__exit__(None, None, None)
        return answer
