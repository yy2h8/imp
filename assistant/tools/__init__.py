"""Assistant tool registry: imp's build_tools plus send_file and the
scheduler tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

from imp.adapters import FileSystemAdapter, HttpClient
from imp.config import Config
from imp.tools import Tool, build_tools

from .schedule import ScheduleJob, UnscheduleJob
from .send_file import SendFile


def build_assistant_tools(
    config: Config,
    fs: FileSystemAdapter,
    prompt_user: Callable[..., Awaitable[str]],
    http: HttpClient,
    sender: Callable[[Path, str], Awaitable[str | None]],
    tz: str,
) -> dict[str, Tool]:
    tools = build_tools(config=config, fs=fs, prompt_user=prompt_user, http=http)
    tools[SendFile.name] = SendFile(
        config=config, fs=fs, prompt_user=prompt_user, sender=sender
    )
    tools[ScheduleJob.name] = ScheduleJob(config=config, tz=tz)
    tools[UnscheduleJob.name] = UnscheduleJob(config=config)
    return tools


__all__ = ["ScheduleJob", "SendFile", "UnscheduleJob", "build_assistant_tools"]
