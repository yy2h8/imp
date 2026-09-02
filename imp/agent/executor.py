"""Run tool batches: read-only tools concurrently, mutating tools sequentially."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from ..entities import ToolCall, ToolMessage
from ..events import AgentEvent, EventType
from ..tools import Tool, ToolResult, execute_call
from .context import Context


def _truncate_tool_output(tool_output: str, max_tool_output: int) -> str:
    if len(tool_output) <= max_tool_output:
        return tool_output
    # cut on a line boundary so line-numbered reads keep a clean resume point
    kept: list[str] = []
    length = 0
    for line in tool_output.splitlines(keepends=True):
        if length + len(line) > max_tool_output:
            break
        kept.append(line)
        length += len(line)
    omitted = len(tool_output) - length
    if not kept:  # single line longer than the budget: fall back to a hard cut
        return tool_output[:max_tool_output] + (
            f"... [truncated, {len(tool_output) - max_tool_output} characters omitted]"
        )
    return "".join(kept) + (
        f"... [truncated after {len(kept)} lines, {omitted} characters omitted; "
        "continue from the last line shown]"
    )


async def execute_tool_batch(
    tools: dict[str, Tool], calls: list[ToolCall], context: Context
) -> AsyncIterator[AgentEvent]:
    """Read-only tools run concurrently; mutating tools run sequentially
    in original order. Results append to the context in tool-call order
    regardless of completion order (deterministic sessions)."""

    async def run(call: ToolCall) -> tuple[ToolCall, ToolResult]:
        result = await execute_call(tools, call.function_name, call.arguments)
        return call, result

    def is_mutating(name: str) -> bool:
        tool = tools.get(name)
        return tool.mutating if tool else False

    reads = [c for c in calls if not is_mutating(c.function_name)]
    writes = [c for c in calls if is_mutating(c.function_name)]

    for call in reads:
        yield AgentEvent(
            type=EventType.TOOL_START,
            token_usage=context.get_usage(),
            tool_name=call.function_name,
            tool_args=call.arguments,
        )
    results: dict[int, ToolResult] = {}  # keyed by object: duplicate call_ids can't collide
    for finished in asyncio.as_completed([run(c) for c in reads]):
        call, result = await finished
        results[id(call)] = result
        yield AgentEvent(
            type=EventType.TOOL_RESULT,
            tool_result=result,
            token_usage=context.get_usage(),
            tool_name=call.function_name,
        )

    for call in writes:
        yield AgentEvent(
            type=EventType.TOOL_START,
            token_usage=context.get_usage(),
            tool_name=call.function_name,
            tool_args=call.arguments,
        )
        result = await execute_call(tools, call.function_name, call.arguments)
        results[id(call)] = result
        yield AgentEvent(
            type=EventType.TOOL_RESULT,
            tool_result=result,
            token_usage=context.get_usage(),
            tool_name=call.function_name,
        )

    for call in calls:
        context.append(
            ToolMessage(
                content=_truncate_tool_output(
                    results[id(call)].content,
                    context.config.max_tool_output,
                ),
                call_id=call.call_id,
            )
        )
