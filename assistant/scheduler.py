"""APScheduler-driven job execution: durable schedules in state.db (via
SQLAlchemyJobStore), each due job as its own asyncio task bounded by a
semaphore, results delivered through the Outbox between interactive turns.

The Job dataclass/schema code from v1 lives on only where the schedule
tools still need it (see tools/schedule.py); this module owns execution.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import aclosing
from dataclasses import dataclass
from datetime import UTC, datetime
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

from .adapters.telegram import send_text
from .db import STATE_DB_NAME, jobs_meta_running, jobs_meta_update, turn_insert
from .outbox import Outbox
from .transcripts import DbSessionWriter

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
    return isinstance(
        getattr(job, "trigger", None), (IntervalTrigger, CronTrigger)
    )


async def run_scheduled_job(schedule_id: str, prompt: str) -> None:
    """Module-level APSerializer target: runs one due job in a fresh session
    and queues the result for delivery between interactive turns."""
    ctx = _CONTEXT
    if ctx is None:  # stale persisted schedule (e.g. after a refactor)
        _LOG.error(
            "job %s cannot run: no JobContext registered; remove the schedule",
            schedule_id,
        )
        return
    async with ctx.semaphore:
        await jobs_meta_update(ctx.db, schedule_id, state="running")
        _LOG.info("job %s started", schedule_id)
        try:
            result, session_id = await _execute(ctx, schedule_id, prompt)
        except Exception as exc:
            _LOG.warning("job %s failed: %s", schedule_id, exc)
            result = f"Job {schedule_id} failed: {exc}"
            await jobs_meta_update(
                ctx.db, schedule_id, state="error", result=result
            )
        else:
            state = (
                "scheduled" if _recurring(ctx.scheduler, schedule_id) else "done"
            )
            await jobs_meta_update(
                ctx.db,
                schedule_id,
                state=state,
                result=result,
                transcript=session_id,
            )
        await ctx.outbox.submit(f"⏰ {schedule_id}\n\n{result}")
        await jobs_meta_update(ctx.db, schedule_id, delivery="queued")
        _LOG.info("job %s finished; result queued for delivery", schedule_id)


async def _execute(ctx: JobContext, schedule_id: str, prompt: str) -> tuple[str, str]:
    """Fresh context, no interactive questions; records the turns row.
    Returns (answer, session_id)."""
    started = time.monotonic()
    system_prompt = ctx.app.build_prompt() + NON_INTERACTIVE_SUFFIX
    writer = DbSessionWriter(ctx.db_path)
    writer.__enter__()
    answer = ""
    in_tokens = out_tokens = 0
    cost: float | None = None
    tools = 0
    ok = True
    try:
        context = Context(
            config=ctx.app.config, system_prompt=system_prompt, writer=writer
        )
        agent = Agent(
            config=ctx.app.config,
            tools={
                name: tool
                for name, tool in ctx.app.agent.tools.items()
                if name != Ask.name
            },
            client=ctx.app.agent.client,
            context=context,
        )
        try:
            async with aclosing(agent.run_turn(prompt)) as events:
                async for event in events:
                    if event.type is EventType.MODEL_RESPONSE:
                        answer = event.quote or ""
                        if event.usage is not None:
                            in_tokens += event.usage.input_tokens
                            out_tokens += event.usage.output_tokens
                            if event.usage.cost_usd is not None:
                                cost = (cost or 0.0) + event.usage.cost_usd
                    elif event.type is EventType.TOOL_START:
                        tools += 1
                    elif event.type is EventType.ERROR and event.error_message:
                        raise RuntimeError(event.error_message)
        except Exception:
            ok = False
            raise
        return answer, writer.session_id
    finally:
        await turn_insert(
            ctx.db,
            ts=datetime.now(UTC).isoformat(),
            kind="job",
            session_id=writer.session_id,
            model=str(ctx.app.config.model),
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            cost_usd=cost,
            tools=tools,
            seconds=time.monotonic() - started,
            ok=ok,
        )
        writer.__exit__(None, None, None)


async def startup_recovery(ctx: JobContext) -> None:
    """Jobs stuck 'running' from a crashed process: report once, directly
    (not through the outbox — no turn may ever run them again)."""
    for row in await jobs_meta_running(ctx.db):
        notice = (
            f"⏰ {row['schedule_id']}\n\nJob interrupted by shutdown; actions "
            "may already have occurred. Reschedule manually."
        )
        try:
            await send_text(ctx.app.bot, ctx.app.chat_id, notice)
        except Exception as exc:  # a failing notice must not block the rest
            _LOG.error("interrupted-job notice failed: %s", exc)
