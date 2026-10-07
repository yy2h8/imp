from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing

from openai import AsyncOpenAI

from ..config import Config
from ..entities import ReasoningMessage, TextMessage, ToolCall, ToolMessage
from ..events import AgentEvent, EventType
from ..tools import Tool
from .context import Context
from .executor import execute_tool_batch
from .model import call_model


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

    async def run_turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        """Run a single turn of the ReAct loop with the given prompt."""

        try:
            async with aclosing(self._run_turn(prompt)) as events:
                async for event in events:
                    yield event
        finally:
            completed = {
                m.call_id for m in self.context.messages if isinstance(m, ToolMessage)
            }
            for message in list(self.context.messages):
                if isinstance(message, ToolCall) and message.call_id not in completed:
                    self.context.append(
                        ToolMessage(
                            call_id=message.call_id,
                            content="Interrupted; execution outcome unknown. Inspect state before retrying actions.",
                        )
                    )

    async def _run_turn(self, prompt: str) -> AsyncIterator[AgentEvent]:
        self._context_warned = False
        self.context.append(TextMessage(role="user", content=prompt))
        async for event in self._compact_and_trim():
            yield event
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

            if not self._context_warned and self.context.is_approaching_limit():
                self._context_warned = True  # one heads-up per turn
                yield AgentEvent(
                    type=EventType.CONTEXT_WARNING,
                    quote="context is approaching the token limit",
                    token_usage=self.context.get_usage(),
                )
            if self.context.tokens > self.config.compact_threshold:
                async for event in self._compact_and_trim():
                    yield event

            try:
                reply = await call_model(
                    self.client, self.config, self.context.messages, self.tools
                )
            except Exception as e:
                yield AgentEvent(
                    type=EventType.ERROR,
                    error_message=f"Model call failed: {e}",
                    token_usage=self.context.get_usage(),
                )
                return

            for message in reply.messages:
                self.context.append(message)
                if isinstance(message, ReasoningMessage) and message.content:
                    yield AgentEvent(
                        type=EventType.REASONING,
                        quote=message.content,
                        token_usage=self.context.get_usage(),
                    )
            yield AgentEvent(
                type=EventType.MODEL_RESPONSE,
                quote=reply.text,
                token_usage=self.context.get_usage(),
                usage=reply.usage,
            )

            if not reply.tool_calls:
                return  # final response, no tool calls, exit the loop

            async with aclosing(
                execute_tool_batch(self.tools, reply.tool_calls, self.context)
            ) as events:
                async for event in events:
                    yield event

            if not self.context.is_within_token_limit():
                async for event in self._compact_and_trim():
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

    async def _compact_and_trim(self) -> AsyncIterator[AgentEvent]:
        saved = self.context.compact()
        if saved > 0:
            yield AgentEvent(
                type=EventType.CONTEXT_COMPACTED,
                quote=f"compacted prior turns (−{saved} tokens)",
                token_usage=self.context.get_usage(),
                context_detail={"saved": saved},
            )
        dropped = self.context.trim()
        if dropped > 0:
            yield AgentEvent(
                type=EventType.CONTEXT_TRIMMED,
                quote=f"trimmed {dropped} old messages",
                token_usage=self.context.get_usage(),
                context_detail={"dropped": dropped},
            )
