"""Agent tools for durable, curated memory entries."""

from __future__ import annotations

from typing import Any, ClassVar

import aiosqlite

from imp.tools.base import Tool, ToolResult

from ..db import memory_all, memory_delete, memory_set


class MemorySet(Tool):
    name = "memory_set"
    description = "Remember or update a durable fact for future sessions."
    instructions = (
        "Store concise, reusable facts or preferences that should survive /new "
        "and restarts. Use a stable key; do not store secrets or transient details."
    )
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "key": {"type": "string", "description": "Stable memory key."},
        "value": {"type": "string", "description": "Concise fact to remember."},
    }
    required: ClassVar[list[str]] = ["key", "value"]

    def __init__(self, db: aiosqlite.Connection | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.db = db

    async def execute(self, key: str, value: str) -> ToolResult:
        if self.db is None:
            return ToolResult(ok=False, content="memory store is unavailable")
        key = key.strip()
        if not key:
            return ToolResult(ok=False, content="memory key must not be empty")
        try:
            await memory_set(self.db, key, value)
        except ValueError as exc:
            return ToolResult(ok=False, content=str(exc))
        return ToolResult(ok=True, content=f"remembered {key!r}")


class MemoryList(Tool):
    name = "memory_list"
    description = "List durable facts and preferences the assistant remembers."
    parameters: ClassVar[dict[str, Any]] = {}
    required: ClassVar[list[str]] = []

    def __init__(self, db: aiosqlite.Connection | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.db = db

    async def execute(self) -> ToolResult:
        if self.db is None:
            return ToolResult(ok=False, content="memory store is unavailable")
        entries = await memory_all(self.db)
        if not entries:
            return ToolResult(ok=True, content="No saved memories.")
        return ToolResult(
            ok=True,
            content="\n".join(f"{key}: {value}" for key, value in reversed(entries)),
        )


class MemoryDelete(Tool):
    name = "memory_delete"
    description = "Forget a saved fact by key."
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "key": {"type": "string", "description": "Memory key to forget."}
    }
    required: ClassVar[list[str]] = ["key"]

    def __init__(self, db: aiosqlite.Connection | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.db = db

    async def execute(self, key: str) -> ToolResult:
        if self.db is None:
            return ToolResult(ok=False, content="memory store is unavailable")
        if not await memory_delete(self.db, key.strip()):
            return ToolResult(ok=False, content=f"no saved memory with key {key!r}")
        return ToolResult(ok=True, content=f"forgot {key!r}")
