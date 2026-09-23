"""Internal asyncio scheduler over jobs/*.json (D8: no host cron, stdlib only).

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
from datetime import UTC, datetime
from pathlib import Path

from imp.agent import Agent, EventType

from .app import AssistantApp, Session
from .jobstore import Job, advance, load_jobs, next_due, save_job

IDLE_POLL_S = 30.0  # re-read jobs/ this often while nothing is due
MAX_SLEEP_S = 3600.0  # re-check at least hourly (clock drift, edited jobs)


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
