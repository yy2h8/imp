"""send_file: deliver workspace files to Telegram."""

from __future__ import annotations

from typing import Any, ClassVar

from imp.tools.base import Tool, ToolResult


class SendFile(Tool):
    name = "send_file"
    description = "Send a file from the workspace to the owner via Telegram."
    instructions = (
        "Paths must resolve inside the workspace — stage deliverables in outbox/. "
        "Use an optional caption to say what the file is; mention the delivery in "
        "your final message."
    )
    mutating = True  # serializes within a batch: one egress at a time
    parameters: ClassVar[dict[str, Any]] = {
        "path": {
            "type": "string",
            "description": "The path to the file relative to the workspace.",
        },
        "caption": {
            "type": "string",
            "description": "Optional caption shown with the file.",
        },
    }
    required: ClassVar[list[str]] = ["path"]

    def __init__(self, sender, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sender = sender  # async (path: Path, caption: str) -> str | None

    async def execute(self, path: str, caption: str = "") -> ToolResult:
        if self.fs is None:
            return ToolResult(ok=False, content="send_file has no filesystem access")
        try:
            resolved = self.fs.resolve_path(path, must_exist=True)
        except FileNotFoundError:
            return ToolResult(
                ok=False,
                content=f"File not found: {path}. Create it first (staging it in "
                "outbox/ is the convention).",
            )
        except ValueError as exc:
            return ToolResult(ok=False, content=f"send_file refused: {exc}")
        if not resolved.is_file():
            return ToolResult(ok=False, content=f"Not a file: {path}")

        try:
            message = await self.sender(resolved, caption)
        except Exception as exc:
            return ToolResult(ok=False, content=f"File delivery failed: {exc}")
        if message is None:
            return ToolResult(
                ok=False, content=f"Telegram rejected the upload of {path}."
            )
        return ToolResult(ok=True, content=f"Sent {path} to the owner.")
