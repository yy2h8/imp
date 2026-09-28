"""Read-only agent tools for scheduled jobs, transcripts, costs and queue."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, ClassVar

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from imp.tools.base import Tool, ToolResult

from ..db import (
    jobs_meta_list,
    queue_count_waiting,
    transcript_search,
    turns_report,
)


def _need_db(db: aiosqlite.Connection | None) -> ToolResult | None:
    if db is None:
        return ToolResult(ok=False, content="state database is unavailable")
    return None


class ListJobs(Tool):
    name = "list_jobs"
    description = "List scheduled jobs, their next fire time and last result."
    parameters: ClassVar[dict[str, Any]] = {}
    required: ClassVar[list[str]] = []

    def __init__(
        self,
        db: aiosqlite.Connection | None = None,
        scheduler: AsyncIOScheduler | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.db = db
        self.scheduler = scheduler

    async def execute(self) -> ToolResult:
        unavailable = _need_db(self.db)
        if unavailable:
            return unavailable
        metadata = {row["schedule_id"]: row for row in await jobs_meta_list(self.db)}
        scheduled = self.scheduler.get_jobs() if self.scheduler is not None else []
        if not scheduled and not metadata:
            return ToolResult(ok=True, content="No scheduled jobs.")
        lines = []
        for job in scheduled:
            row = metadata.get(job.id, {})
            next_run = getattr(job, "next_run_time", None)
            next_text = next_run.isoformat() if next_run else "no next fire"
            state = row.get("state", "scheduled")
            label = row.get("label", "")
            result = row.get("result", "")[:200]
            lines.append(
                f"{job.id} · {state} · {next_text}"
                + (f" · {label}" if label else "")
                + (f" · last: {result}" if result else "")
            )
        for schedule_id, row in metadata.items():
            if schedule_id not in {job.id for job in scheduled}:
                result = row.get("result", "")[:200]
                lines.append(
                    f"{schedule_id} · {row['state']}"
                    + (f" · {row['label']}" if row.get("label") else "")
                    + (f" · last: {result}" if result else "")
                )
        return ToolResult(ok=True, content="\n".join(lines))


class SearchTranscripts(Tool):
    name = "search_transcripts"
    description = "Search archived conversation transcripts for a phrase."
    parameters: ClassVar[dict[str, Any]] = {
        "query": {"type": "string", "description": "Text to find in transcripts."},
        "limit": {
            "type": "integer",
            "description": "Maximum results (1..50, default 10).",
        },
    }
    required: ClassVar[list[str]] = ["query"]

    def __init__(
        self, db: aiosqlite.Connection | None = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.db = db

    async def execute(self, query: str, limit: int = 10) -> ToolResult:
        unavailable = _need_db(self.db)
        if unavailable:
            return unavailable
        if not query.strip():
            return ToolResult(ok=False, content="query must not be empty")
        if type(limit) is not int or not 1 <= limit <= 50:
            return ToolResult(ok=False, content="limit must be an integer in 1..50")
        rows = await transcript_search(self.db, query.strip(), limit)
        if not rows:
            return ToolResult(ok=True, content="No transcript matches.")
        lines = []
        for session_id, raw in rows:
            try:
                message = json.loads(raw)
                role = message.get("role", message.get("type", "message"))
                content = message.get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        str(item.get("text", item)) if isinstance(item, dict) else str(item)
                        for item in content
                    )
                lines.append(f"{session_id} · {role}: {str(content)[:500]}")
            except (ValueError, TypeError):
                lines.append(f"{session_id}: {raw[:500]}")
        return ToolResult(ok=True, content="\n".join(lines))


class CostReport(Tool):
    name = "cost_report"
    description = "Report turn count, token totals and spend for a period."
    parameters: ClassVar[dict[str, Any]] = {
        "period": {
            "type": "string",
            "enum": ["day", "week", "month", "all"],
            "description": "Reporting window (default all).",
        }
    }
    required: ClassVar[list[str]] = []

    def __init__(
        self, db: aiosqlite.Connection | None = None, **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.db = db

    async def execute(self, period: str = "all") -> ToolResult:
        unavailable = _need_db(self.db)
        if unavailable:
            return unavailable
        try:
            report = await turns_report(self.db, period)
        except ValueError as exc:
            return ToolResult(ok=False, content=str(exc))
        content = (
            f"turns {report['turns']} · in {_short(report['in_tokens'])}"
            f" · out {_short(report['out_tokens'])} · ${report['cost_usd']:.4f}"
        )
        return ToolResult(ok=True, content=content)


def _short(value: int) -> str:
    return f"{value / 1000:.0f}k" if value >= 1000 else str(value)


class QueueStatus(Tool):
    name = "queue_status"
    description = "Inspect the waiting request queue and whether a turn is active."
    parameters: ClassVar[dict[str, Any]] = {}
    required: ClassVar[list[str]] = []

    def __init__(
        self,
        db: aiosqlite.Connection | None = None,
        is_turn_active: Callable[[], bool] | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.db = db
        self.is_turn_active = is_turn_active or (lambda: False)

    async def execute(self) -> ToolResult:
        unavailable = _need_db(self.db)
        if unavailable:
            return unavailable
        waiting = await queue_count_waiting(self.db)
        state = "turn active" if self.is_turn_active() else "no active turn"
        return ToolResult(ok=True, content=f"{waiting} waiting · {state}")
