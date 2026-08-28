from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from ..adapters import FileSystemAdapter, HttpClient
from ..config import Config


@dataclass(slots=True, frozen=True)
class ToolResult:
    ok: bool
    content: str
    diff: str | None = None


class Tool(ABC):
    name: ClassVar[str]
    description: ClassVar[str]
    instructions: ClassVar[str] = ""
    parameters: ClassVar[dict[str, Any]]
    required: ClassVar[list[str]] = []
    # mutating tools serialize within a tool batch (concurrent reads stay parallel)
    mutating: ClassVar[bool] = False

    def __init__(
        self,
        config: Config | None = None,
        fs: FileSystemAdapter | None = None,
        prompt_user: Callable[[str], Awaitable[str]] | None = None,
        http: HttpClient | None = None,
    ) -> None:
        self.config = config
        self.fs = fs
        self.prompt_user = prompt_user
        self.http = http

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        for attr in ("name", "description", "parameters"):
            if not hasattr(cls, attr):
                raise TypeError(f"{cls.__name__} must define class attribute '{attr}'")

    async def _ask_approval(self, message: str) -> bool:
        if self.config.auto_approve:
            return True
        response = await self.prompt_user(message)
        return response.strip().lower() in {"y", "yes"}

    def openai_schema(self) -> dict[str, Any]:
        """Return the tool's schema in the shape most providers expect."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }

    @abstractmethod
    async def execute(self, **kwargs: Any) -> ToolResult: ...
