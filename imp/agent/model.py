"""Speak the OpenAI Responses API: build a request, parse output items."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI

from ..config import Config
from ..entities import (
    AssistantMessage,
    ConversationMessage,
    ReasoningMessage,
    ToolCall,
)
from ..tools import Tool


@dataclass(slots=True, frozen=True)
class ModelReply:
    messages: list[ConversationMessage]  # output items in order, replayable
    tool_calls: list[ToolCall]
    text: str | None


def _parse_output(output: Iterable[Any]) -> ModelReply:
    # unknown item types (e.g. built-in tool calls) are dropped by design:
    # imp replays only what it understands — a provider that needs a dropped
    # item back fails at the next model call, surfaced as an ERROR event
    messages: list[ConversationMessage] = []
    tool_calls: list[ToolCall] = []
    text: str | None = None
    for raw in output:
        item = raw.model_dump(exclude_none=True)
        kind = item.get("type")
        if kind == "reasoning":
            messages.append(ReasoningMessage.parse(item))
        elif kind == "message":
            message = AssistantMessage.parse(item)
            text = message.content
            messages.append(message)
        elif kind == "function_call":
            call = ToolCall.parse(item)
            messages.append(call)
            tool_calls.append(call)
    return ModelReply(messages=messages, tool_calls=tool_calls, text=text)


async def call_model(
    client: AsyncOpenAI,
    config: Config,
    messages: list[ConversationMessage],
    tools: dict[str, Tool],
) -> ModelReply:
    """Call the Responses API statelessly; imp owns the context window."""
    request: dict[str, Any] = {
        "model": config.model,
        # stateless replay needs the encrypted payload; reasoning items
        # without one can't be replayed and are dropped from the request —
        # the model simply re-reasons on the next iteration
        "input": [
            m.serialize()
            for m in messages
            if not (
                isinstance(m, ReasoningMessage) and "encrypted_content" not in m.item
            )
        ],
        "tools": [t.openai_schema() for t in tools.values()],
        "store": False,  # imp owns the context; items replay statelessly
    }
    if config.reasoning_effort:
        request["include"] = ["reasoning.encrypted_content"]
        request["reasoning"] = {
            "effort": config.reasoning_effort,
            "summary": "auto",
        }
    response = await client.responses.create(**request)
    return _parse_output(response.output)
