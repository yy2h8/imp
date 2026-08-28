from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar

from imp.agent import Agent, EventType
from imp.agent.context import Context
from imp.entities import ToolMessage
from imp.tools import Tool, ToolResult
from imp.tools.fs import WriteFile


def sdk_tool_call(call: dict):
    return SimpleNamespace(
        id=call["id"],
        function=SimpleNamespace(name=call["name"], arguments=call["arguments"]),
    )


def sdk_message(content: str | None, tool_calls: list[dict] | None = None):
    calls = [sdk_tool_call(c) for c in tool_calls or []]
    return SimpleNamespace(content=content, tool_calls=calls or None)


def completion(message: object):
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class StubClient:
    """Minimal AsyncOpenAI stand-in: plays scripted responses, records calls.
    Scripted items may be Exceptions (raised) or SDK-shaped messages."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any):
        self.calls.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return completion(item)


class FakeTool(Tool):
    name = "fake"
    description = "fake tool"
    parameters: ClassVar[dict[str, Any]] = {}

    def __init__(self, result: ToolResult, delay: float = 0.0, name: str = "fake"):
        self.name = name
        self.result = result
        self.delay = delay

    async def execute(self, **kwargs: Any) -> ToolResult:
        await asyncio.sleep(self.delay)
        return self.result


def make_agent(config, tools: dict[str, Tool], responses: list):
    client = StubClient(responses)
    agent = Agent(
        config=config,
        tools=tools,
        client=client,
        context=Context(config=config, system_prompt="sys"),
    )
    return agent, client


async def collect(agent: Agent, prompt: str):
    return [event async for event in agent.run_turn(prompt)]


async def test_text_only_turn(config):
    agent, client = make_agent(config, {}, [sdk_message("Done")])
    events = await collect(agent, "hi")
    assert [e.type for e in events] == [EventType.THINKING, EventType.MODEL_RESPONSE]
    assert events[-1].quote == "Done"

    names = [type(m).__name__ for m in agent.context.messages]
    assert names == ["TextMessage", "TextMessage", "AssistantMessage"]

    sent = client.calls[0]["messages"]
    assert sent[0]["role"] == "system"
    assert sent[1] == {"role": "user", "content": "hi"}


async def test_results_append_in_tool_call_order(config):
    tools = {
        "slow": FakeTool(ToolResult(ok=True, content="slow"), delay=0.05, name="slow"),
        "fast": FakeTool(ToolResult(ok=True, content="fast"), name="fast"),
    }
    batch = sdk_message(
        None,
        [
            {"id": "1", "name": "slow", "arguments": {}},
            {"id": "2", "name": "fast", "arguments": {}},
        ],
    )
    agent, _ = make_agent(config, tools, [batch, sdk_message("done")])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.MODEL_RESPONSE

    tool_messages = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in tool_messages] == ["1", "2"]
    assert [m.content for m in tool_messages] == ["slow", "fast"]


async def test_declined_mutating_tool_recorded(config, fs):
    tools = {
        "write_file": WriteFile(config=config, fs=fs, prompt_user=awaitable_no()),
    }
    batch = sdk_message(
        None,
        [
            {
                "id": "1",
                "name": "write_file",
                "arguments": {"path": "x.txt", "content": "d"},
            }
        ],
    )
    agent, _ = make_agent(config, tools, [batch, sdk_message("ok")])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.MODEL_RESPONSE

    tool_messages = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
    assert "not approved" in tool_messages[0].content
    assert not (config.workspace / "x.txt").exists()


def awaitable_no():
    async def prompt_user(message: str) -> str:
        return "n"

    return prompt_user


async def test_model_error_aborts(config):
    agent, client = make_agent(config, {}, [RuntimeError("boom")])
    events = await collect(agent, "hi")
    assert [e.type for e in events] == [EventType.THINKING, EventType.ERROR]
    assert "Model call failed" in events[-1].error_message
    assert len(client.calls) == 1


async def test_context_overflow_before_start(config):
    config.max_context = 100
    agent, client = make_agent(config, {}, [sdk_message("nope")])
    events = await collect(agent, "y" * 10_000)
    assert [e.type for e in events] == [EventType.ERROR]
    assert "token limit" in events[0].error_message
    assert client.calls == []


async def test_context_overflow_after_tools(config):
    config.max_context = 500
    tools = {"big": FakeTool(ToolResult(ok=True, content="x" * 100_000), name="big")}
    batch = sdk_message(None, [{"id": "1", "name": "big", "arguments": {}}])
    agent, _ = make_agent(config, tools, [batch, sdk_message("never")])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.ERROR
    assert "token limit" in events[-1].error_message


async def test_max_iterations_reached(config):
    config.max_iterations = 2
    batch = sdk_message(None, [{"id": "1", "name": "fake", "arguments": {}}])
    agent, _ = make_agent(
        config, {"fake": FakeTool(ToolResult(ok=True, content="r"))}, [batch] * 5
    )
    events = await collect(agent, "go")
    assert events[-1].type is EventType.ERROR
    assert "iteration limit" in events[-1].error_message
