from __future__ import annotations

from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from ..config import Config
from ..entities import ReasoningMessage, TextMessage
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
            )

            if not reply.tool_calls:
                return  # final response, no tool calls, exit the loop

            async for event in execute_tool_batch(
                self.tools, reply.tool_calls, self.context
            ):
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
