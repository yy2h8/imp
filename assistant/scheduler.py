"""APScheduler-driven job execution: durable schedules in state.db (via
SQLAlchemyJobStore), each due job as its own asyncio task bounded by a
semaphore, results delivered through the Outbox between interactive turns.

The Job dataclass/schema code from v1 lives on only where the schedule
tools still need it (see tools/schedule.py); this module owns execution.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import URL

from imp.agent import Agent, Context, EventType
from imp.tools.ask import Ask

from .adapters.ui import turn_summary
from .db import STATE_DB_NAME, recover_runs
from .outbox import Outbox
from .transcripts import DbSessionWriter
from .turns import Run, error_detail

_LOG = logging.getLogger(__name__)

NON_INTERACTIVE_SUFFIX = (
    "\n\nThis is a non-interactive scheduled job. The ask tool is unavailable. "
    "Do not ask questions or wait for replies. If essential information "
    "is missing, report what prevented completion."
)


@dataclass(slots=True)
class JobContext:
    """Everything a running job needs from the composition root."""

    app: object  # AssistantApp: config, agent (tools, client), build_prompt, chat_id, bot
    outbox: Outbox
    semaphore: asyncio.Semaphore
    db: aiosqlite.Connection
    db_path: Path
    scheduler: AsyncIOScheduler


_CONTEXT: JobContext | None = None
_RUNNING: set[asyncio.Task] = set()


def set_context(ctx: JobContext | None) -> None:
    global _CONTEXT
    _CONTEXT = ctx


def build_scheduler(home: Path, tz: str) -> AsyncIOScheduler:
    """Durable scheduler: jobstore table lives in the same state.db."""
    url = URL.create("sqlite", database=str(home / STATE_DB_NAME))
    store = SQLAlchemyJobStore(
        url=url, engine_options={"connect_args": {"timeout": 30}}
    )
    return AsyncIOScheduler(
        timezone=ZoneInfo(tz),
        jobstores={"default": store},
        job_defaults={
            "misfire_grace_time": 60,
            "coalesce": True,
            "max_instances": 1,
        },
    )


def _recurring(scheduler: AsyncIOScheduler, schedule_id: str) -> bool:
    job = scheduler.get_job(schedule_id)
    return isinstance(getattr(job, "trigger", None), (IntervalTrigger, CronTrigger))


async def run_scheduled_job(schedule_id: str, prompt: str) -> None:
    ctx = _CONTEXT
    if ctx is None:
        _LOG.error("job %s cannot run: no JobContext", schedule_id)
        return
    task = asyncio.current_task()
    _RUNNING.add(task)
    try:
        async with ctx.semaphore:
            writer = DbSessionWriter(ctx.db_path)
            secrets = (
                ctx.app.config.api_key,
                getattr(getattr(ctx.app, "assistant", None), "bot_token", ""),
            )
            run = await Run.start(
                ctx.db_path,
                kind="job",
                model=ctx.app.config.model,
                session_id=writer.session_id,
                schedule_id=schedule_id,
                secrets=secrets,
            )
            status = "completed"
            answer = ""
            try:
                if run.cancelled_during_start:
                    raise asyncio.CancelledError
                await asyncio.to_thread(writer.__enter__)
                answer = await _execute(ctx, run, prompt, writer)
                if run.error:
                    status = "failed"
                elif not answer.strip():
                    status = "failed"
                    run.error, run.error_code = (
                        "Model returned no final answer",
                        "empty_response",
                    )
            except asyncio.CancelledError:
                status = "interrupted"
                run.error, run.error_code = (
                    "Process stopped before completion",
                    "interrupted",
                )
            except Exception as exc:
                status = "failed"
                run.error, run.error_code = error_detail(exc, secrets), "runtime_error"
            finally:
                await asyncio.to_thread(writer.__exit__, None, None, None)
            text = (
                answer
                if status == "completed"
                else f"Задание {schedule_id} завершилось с ошибкой: {run.error}"
            )
            summary = turn_summary(
                run.tools,
                int(run.seconds),
                status == "completed",
                run.cost,
                run.cost_complete,
            )
            state = (
                ("scheduled" if _recurring(ctx.scheduler, schedule_id) else "done")
                if status == "completed"
                else ("interrupted" if status == "interrupted" else "error")
            )
            await run.finish(
                status,
                ctx.app.chat_id,
                f"⏰ {schedule_id}\n\n{text}\n\n{summary}",
                job_state=state,
                job_result=text,
            )
            ctx.outbox.wake()
            if status == "interrupted":
                raise asyncio.CancelledError
    finally:
        _RUNNING.discard(task)


async def _execute(
    ctx: JobContext, run: Run, prompt: str, writer: DbSessionWriter
) -> str:
    system_prompt = await asyncio.to_thread(ctx.app.build_prompt)
    context = Context(
        config=ctx.app.config,
        system_prompt=system_prompt + NON_INTERACTIVE_SUFFIX,
        writer=writer,
    )
    agent = Agent(
        config=ctx.app.config,
        tools={
            name: tool for name, tool in ctx.app.agent.tools.items() if name != Ask.name
        },
        client=ctx.app.agent.client,
        context=context,
    )
    answer = ""
    async with aclosing(agent.run_turn(prompt)) as events:
        async for event in events:
            await run.observe(event)
            if event.type is EventType.MODEL_RESPONSE:
                answer = event.quote or ""
    return answer


async def startup_recovery(ctx: JobContext) -> None:
    await recover_runs(ctx.db_path, ctx.app.chat_id)
    ctx.outbox.wake()


async def shutdown_jobs() -> None:
    tasks = list(_RUNNING)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
