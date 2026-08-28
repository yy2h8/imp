from __future__ import annotations

from typing import Any, ClassVar

from .base import Tool, ToolResult


class Ask(Tool):
    name = "ask"
    description = "Ask the user a specific question and get their response."
    instructions = "Use the ask tool only when essential information truly cannot be inferred and a wrong choice could cause harm."
    parameters: ClassVar[dict[str, Any]] = {
        "question": {
            "type": "string",
            "description": "The question to ask the user.",
        }
    }
    required: ClassVar[list[str]] = ["question"]

    async def execute(self, question: str) -> ToolResult:
        answer = await self.prompt_user(question)
        answer = (
            f"User answered: {answer}"
            if answer.strip()
            else ("User gave no answer; proceed with your best judgment.")
        )
        return ToolResult(ok=True, content=answer)
