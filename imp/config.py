from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_MODEL: str = "gpt-5-mini"
DEFAULT_BASE_URL: str = "https://api.openai.com/v1"
REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)
DEFAULT_WORKSPACE: str = "."
DEFAULT_MAX_ITERATIONS: int = 50
DEFAULT_MAX_CONTEXT: int = 128_000
DEFAULT_COMMAND_TIMEOUT: int = 180
DEFAULT_NETWORK_TIMEOUT: int = 360
DEFAULT_MAX_TOOL_OUTPUT: int = 100_000
DEFAULT_MAX_TOOL_DISPLAY_LINES: int = 10
DEFAULT_MAX_REASONING_DISPLAY_LINES: int = 10
DEFAULT_MAX_HTTP_BYTES: int = 10_000_000


def _int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
        if value <= 0:
            raise ValueError
        return value
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc


def _truthy(name: str) -> bool:
    value = os.getenv(name)
    return (value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _effort(name: str) -> str | None:
    value = (os.getenv(name) or "").strip().lower() or None
    if value is not None and value not in REASONING_EFFORTS:
        raise ValueError(f"{name} must be one of: {', '.join(REASONING_EFFORTS)}")
    return value


@dataclass(slots=True)
class Config:
    api_key: str
    workspace: Path
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    max_context: int = DEFAULT_MAX_CONTEXT
    command_timeout: int = DEFAULT_COMMAND_TIMEOUT
    network_timeout: int = DEFAULT_NETWORK_TIMEOUT
    max_tool_output: int = DEFAULT_MAX_TOOL_OUTPUT
    max_tool_display_lines: int = DEFAULT_MAX_TOOL_DISPLAY_LINES
    max_reasoning_display_lines: int = DEFAULT_MAX_REASONING_DISPLAY_LINES
    max_http_bytes: int = DEFAULT_MAX_HTTP_BYTES
    auto_approve: bool = False  # Can also be set to true via cli argument
    brave_api_key: str | None = None  # Optional Brave Search API key for web search
    reasoning_effort: str | None = (
        None  # Optional reasoning effort for reasoning models
    )

    @classmethod
    def from_env(cls, auto_approve: bool | None = None) -> Config:
        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise ValueError(
                "OPENAI_API_KEY is not set. Export your provider API key and retry."
            )

        root = (
            Path(os.getenv("IMP_WORKSPACE") or DEFAULT_WORKSPACE).expanduser().resolve()
        )
        if not root.is_dir():
            raise ValueError(f"Workspace is not a directory: {root}")

        return cls(
            api_key=key,
            workspace=root,
            model=os.getenv("OPENAI_MODEL") or DEFAULT_MODEL,
            base_url=os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL,
            max_iterations=_int("IMP_MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS),
            max_context=_int("IMP_MAX_CONTEXT", DEFAULT_MAX_CONTEXT),
            command_timeout=_int("IMP_COMMAND_TIMEOUT", DEFAULT_COMMAND_TIMEOUT),
            network_timeout=_int("IMP_NETWORK_TIMEOUT", DEFAULT_NETWORK_TIMEOUT),
            max_tool_output=_int("IMP_MAX_TOOL_OUTPUT", DEFAULT_MAX_TOOL_OUTPUT),
            max_tool_display_lines=_int(
                "IMP_MAX_TOOL_DISPLAY_LINES", DEFAULT_MAX_TOOL_DISPLAY_LINES
            ),
            max_reasoning_display_lines=_int(
                "IMP_MAX_REASONING_DISPLAY_LINES", DEFAULT_MAX_REASONING_DISPLAY_LINES
            ),
            max_http_bytes=_int("IMP_MAX_HTTP_BYTES", DEFAULT_MAX_HTTP_BYTES),
            auto_approve=auto_approve
            if auto_approve is not None
            else _truthy("IMP_AUTO_APPROVE"),
            brave_api_key=os.getenv("BRAVE_API_KEY"),
            reasoning_effort=_effort("IMP_REASONING_EFFORT"),
        )
