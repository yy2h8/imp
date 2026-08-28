from __future__ import annotations

import asyncio
from typing import Any, ClassVar

from .base import Tool, ToolResult


class ListDir(Tool):
    name = "list_dir"
    description = "List the contents of a directory within the workspace."
    instructions = "When listing a directory or reading a file some entries may be skipped (e.g. .git, .env)."
    parameters: ClassVar[dict[str, Any]] = {
        "path": {
            "type": "string",
            "description": "The path to the directory relative to the workspace.",
        },
        "level": {
            "type": "integer",
            "description": "Depth level for recursive listing.",
        },
    }
    required: ClassVar[list[str]] = ["path"]

    async def execute(self, path: str, level: int = 3) -> ToolResult:
        entries = await asyncio.to_thread(self.fs.list_directory, path, level=level)
        return ToolResult(ok=True, content="\n".join(entries))


class ReadFile(Tool):
    name = "read_file"
    description = "Read the contents of a text file within the workspace."
    instructions = (
        "read_file prefixes each line with its 1-based line number and ends with a "
        "`(lines X-Y of N)` footer; these are annotations, not file content. "
        "Use start_line/end_line to page through large files."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "path": {
            "type": "string",
            "description": "The path to the file relative to the workspace.",
        },
        "start_line": {
            "type": "integer",
            "description": "The starting line number (1-based).",
        },
        "end_line": {
            "type": "integer",
            "description": "The ending line number (1-based, inclusive).",
        },
    }
    required: ClassVar[list[str]] = ["path"]

    async def execute(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> ToolResult:
        content = await asyncio.to_thread(
            self.fs.read_text_file,
            path,
            start_line=start_line,
            end_line=end_line,
            line_numbers=True,
        )
        return ToolResult(ok=True, content=content)


class WriteFile(Tool):
    name = "write_file"
    description = "Write content to a text file within the workspace. Creates the file if it does not exist, overwrites if it does."
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "path": {
            "type": "string",
            "description": "The path to the file relative to the workspace.",
        },
        "content": {
            "type": "string",
            "description": "The content to write to the file.",
        },
    }
    required: ClassVar[list[str]] = ["path", "content"]

    async def execute(self, path: str, content: str) -> ToolResult:
        if not await self._ask_approval(f"Allow {self.name} on '{path}' (y/n)? "):
            return ToolResult(
                ok=False, content=f"{self.name} on '{path}' was not approved."
            )
        message = await asyncio.to_thread(self.fs.write_text_file, path, content)
        return ToolResult(ok=True, content=message)


class StrReplace(Tool):
    name = "str_replace"
    description = "Replace a string in a text file within the workspace."
    instructions = (
        "Use str_replace for existing files; use write_file for new files or full "
        "rewrites. old and new must be exact raw file text, without the line-number "
        "prefixes and footer read_file adds."
    )
    mutating = True
    parameters: ClassVar[dict[str, Any]] = {
        "path": {
            "type": "string",
            "description": "The path to the file relative to the workspace.",
        },
        "old": {"type": "string", "description": "The string to be replaced."},
        "new": {"type": "string", "description": "The string to replace with."},
    }
    required: ClassVar[list[str]] = ["path", "old", "new"]

    async def execute(self, path: str, old: str, new: str) -> ToolResult:
        if not await self._ask_approval(f"Allow {self.name} on '{path}' (y/n)? "):
            return ToolResult(
                ok=False, content=f"{self.name} on '{path}' was not approved."
            )
        diff = await asyncio.to_thread(self.fs.str_replace, path, old, new)
        return ToolResult(
            ok=True, content=f"Replaced 1 occurrence in {path}", diff=diff
        )
