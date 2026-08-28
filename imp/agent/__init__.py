from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from openai import AsyncOpenAI

from ..config import Config
from ..entities import AssistantMessage, TextMessage, ToolCall, ToolMessage
from ..tools import Tool, ToolResult, execute_call
from .context import Context
from .prompt import build_system_prompt


class EventType(Enum):
    THINKING = auto()
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


def _truncate_tool_output(tool_output: str, max_tool_output: int) -> str:
    if len(tool_output) > max_tool_output:
        return (
            tool_output[:max_tool_output]
            + f"... [truncated, {len(tool_output) - max_tool_output} characters omitted]"
        )
    return tool_output


class Agent:
    def __init__(
        self,
        config: Config,
        tools: dict[str, Tool],
        client: AsyncOpenAI,
        context: Context,
    ) -> None:
        self.config = config
        self.tools = tools
        self.context = context
        self.client = client

    async def _execute_tools(
        self, tool_calls: list[ToolCall]
    ) -> AsyncIterator[AgentEvent]:
        """Read-only tools run concurrently; mutating tools run sequentially
        in original order. Results append to the context in tool-call order
        regardless of completion order (deterministic sessions)."""

        async def run(call: ToolCall) -> tuple[ToolCall, ToolResult]:
            result = await execute_call(self.tools, call.function_name, call.arguments)
            return call, result

        def is_mutating(name: str) -> bool:
            tool = self.tools.get(name)
            return tool.mutating if tool else False

        reads = [c for c in tool_calls if not is_mutating(c.function_name)]
        writes = [c for c in tool_calls if is_mutating(c.function_name)]

        for call in reads:
            yield AgentEvent(
                type=EventType.TOOL_START,
                token_usage=self.context.get_usage(),
                tool_name=call.function_name,
                tool_args=call.arguments,
            )
        results: dict[str, ToolResult] = {}
        for finished in asyncio.as_completed([run(c) for c in reads]):
            call, result = await finished
            results[call.tool_call_id] = result
            yield AgentEvent(
                type=EventType.TOOL_RESULT,
                tool_result=result,
                token_usage=self.context.get_usage(),
                tool_name=call.function_name,
            )

        for call in writes:
            yield AgentEvent(
                type=EventType.TOOL_START,
                token_usage=self.context.get_usage(),
                tool_name=call.function_name,
                tool_args=call.arguments,
            )
            result = await execute_call(self.tools, call.function_name, call.arguments)
            results[call.tool_call_id] = result
            yield AgentEvent(
                type=EventType.TOOL_RESULT,
                tool_result=result,
                token_usage=self.context.get_usage(),
                tool_name=call.function_name,
            )

        for call in tool_calls:
            self.context.append(
                ToolMessage(
                    content=_truncate_tool_output(
                        results[call.tool_call_id].content,
                        self.config.max_tool_output,
                    ),
                    tool_call_id=call.tool_call_id,
                )
            )

    async def run_turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Run a single turn of the ReAct loop with the given prompt."""

        self.context.append(TextMessage(role="user", content=prompt))
        if not self.context.is_within_token_limit():
            yield AgentEvent(
                type=EventType.ERROR,
                error_message="Context exceeds token limit.",
                token_usage=self.context.get_usage(),
            )
            return

        for _ in range(self.config.max_iterations):
            yield AgentEvent(
                type=EventType.THINKING, token_usage=self.context.get_usage()
            )

            try:
                model_response = await self.client.chat.completions.create(
                    model=self.config.model,
                    messages=[m.serialize() for m in self.context.messages],
                    tools=[t.openai_schema() for t in self.tools.values()],
                )
                model_message = AssistantMessage.parse(
                    model_response.choices[0].message
                )
                yield AgentEvent(
                    type=EventType.MODEL_RESPONSE,
                    quote=model_message.content,
                    token_usage=self.context.get_usage(),
                )
                self.context.append(model_message)
            except Exception as e:
                yield AgentEvent(
                    type=EventType.ERROR,
                    error_message=f"Model call failed: {e}",
                    token_usage=self.context.get_usage(),
                )
                return

            if not model_message.tool_calls:
                return  # final response, no tool calls, exit the loop

            async for event in self._execute_tools(model_message.tool_calls):
                yield event

            if not self.context.is_within_token_limit():
                yield AgentEvent(
                    type=EventType.ERROR,
                    error_message="Context exceeds token limit after tool calls.",
                    token_usage=self.context.get_usage(),
                )
                return

        yield AgentEvent(
            type=EventType.ERROR,
            error_message="Maximum iteration limit reached.",
            token_usage=self.context.get_usage(),
        )


__all__ = [
    "Agent",
    "AgentEvent",
    "Context",
    "EventType",
    "build_system_prompt",
]
