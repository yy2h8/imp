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
    CONTEXT_COMPACTED = auto()
    CONTEXT_TRIMMED = auto()
    CONTEXT_WARNING = auto()
    ERROR = auto()


@dataclass(slots=True, frozen=True)
class Usage:
    """Provider-reported usage for one model call; cost_usd is OpenRouter's
    billed USD amount and is None when the provider does not report one."""

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost_usd: float | None


@dataclass(slots=True, frozen=True)
class AgentEvent:
    type: EventType
    token_usage: tuple[int, int]
    quote: str | None = None
    tool_result: ToolResult | None = None
    error_message: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    usage: Usage | None = None
    # Structured payload for CONTEXT_* events: e.g. {"saved": 626661}, {"dropped": 6}
    context_detail: dict[str, int] | None = None
