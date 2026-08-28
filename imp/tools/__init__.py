from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..adapters import FileSystemAdapter, HttpClient
from ..config import Config
from .ask import Ask
from .base import Tool, ToolResult
from .brave_search import WebSearch
from .fetch import WebFetch
from .fs import ListDir, ReadFile, StrReplace, WriteFile
from .shell import RunShell


def build_tools(
    config: Config,
    fs: FileSystemAdapter,
    prompt_user: Callable[[str], Awaitable[str]],
    http: HttpClient,
) -> dict[str, Tool]:
    tools = {
        tool.name: tool
        for tool in [
            RunShell(config=config, prompt_user=prompt_user),
            Ask(prompt_user=prompt_user),
            ListDir(fs=fs),
            ReadFile(fs=fs),
            WriteFile(config=config, fs=fs, prompt_user=prompt_user),
            StrReplace(config=config, fs=fs, prompt_user=prompt_user),
            WebFetch(http=http),
        ]
    }
    if config.brave_api_key:
        tools[WebSearch.name] = WebSearch(config=config, http=http)
    return tools


async def execute_call(
    tools: dict[str, Tool], tool_name: str, args: dict[str, Any]
) -> ToolResult:
    tool = tools.get(tool_name)
    if not tool:
        return ToolResult(ok=False, content=f"Tool not found: {tool_name}")
    try:
        return await tool.execute(**args)
    except Exception as e:
        return ToolResult(ok=False, content=f"{tool_name} failed: {e}")


__all__ = ["Tool", "ToolResult", "build_tools", "execute_call"]
