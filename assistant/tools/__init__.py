"""Assistant tool registry: imp's build_tools plus send_file and the
scheduler tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from imp.adapters import FileSystemAdapter, HttpClient
from imp.config import Config
from imp.tools import Tool, build_tools

from .memory import MemoryDelete, MemoryList, MemorySet
from .schedule import ScheduleJob, UnscheduleJob
from .send_file import SendFile


def build_assistant_tools(
    config: Config,
    fs: FileSystemAdapter,
    prompt_user: Callable[..., Awaitable[str]],
    http: HttpClient,
    sender: Callable[[Path, str], Awaitable[str | None]],
    tz: str,
    scheduler: AsyncIOScheduler | None = None,
    db: aiosqlite.Connection | None = None,
) -> dict[str, Tool]:
    tools = build_tools(config=config, fs=fs, prompt_user=prompt_user, http=http)
    tools[SendFile.name] = SendFile(
        config=config, fs=fs, prompt_user=prompt_user, sender=sender
    )
    tools[ScheduleJob.name] = ScheduleJob(
        config=config, tz=tz, scheduler=scheduler, db=db
    )
    tools[UnscheduleJob.name] = UnscheduleJob(
        config=config, scheduler=scheduler, db=db
    )
    tools[MemorySet.name] = MemorySet(config=config, db=db)
    tools[MemoryList.name] = MemoryList(config=config, db=db)
    tools[MemoryDelete.name] = MemoryDelete(config=config, db=db)
    return tools


__all__ = [
    "MemoryDelete",
    "MemoryList",
    "MemorySet",
    "ScheduleJob",
    "SendFile",
    "UnscheduleJob",
    "build_assistant_tools",
]
