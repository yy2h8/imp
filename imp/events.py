from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # annotation-only: keeps events free of runtime dependencies
    from ..tools import ToolResult


class EventType(Enum):
    THINKING = auto()
    REASONING = auto()
    MODEL_RESPONSE = auto()
    TOOL_START = auto()
    TOOL_RESULT = auto()
    ERROR = auto()


@dataclass(slots=True, frozen=True)
class AgentEvent:
    type: EventType
    token_usage: tuple[int, int]
    quote: str | None = None
    tool_result: ToolResult | None = None
    error_message: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
