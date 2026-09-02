from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, ClassVar

from imp.agent import Agent, EventType
from imp.agent.context import Context
from imp.agent.executor import _truncate_tool_output
from imp.entities import ReasoningMessage, ToolMessage
from imp.tools import Tool, ToolResult
from imp.tools.fs import WriteFile


def sdk_item(data: dict):
    """Stand-in for an SDK output item; the agent only calls model_dump()."""
    return SimpleNamespace(model_dump=lambda **_: data)


def message_item(text: str):
    return sdk_item(
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": text}],
        }
    )


def reasoning_item(
    summary: str | None = None, text: str | None = None, encrypted: bool = True
):
    data: dict[str, Any] = {"type": "reasoning", "id": "rs_1"}
    if encrypted:
        data["encrypted_content"] = "enc"
    if text:
        data["content"] = [{"type": "reasoning_text", "text": text}]
    if summary:
        data["summary"] = [{"type": "summary_text", "text": summary}]
    return sdk_item(data)


def function_call_item(call_id: str, name: str, arguments: dict):
    return sdk_item(
        {
            "type": "function_call",
            "id": f"fc_{call_id}",
            "call_id": call_id,
            "name": name,
            "arguments": arguments,
            "status": "completed",
        }
    )


def response(items: list):
    return SimpleNamespace(output=items)


class StubClient:
    """Minimal AsyncOpenAI stand-in: plays scripted responses, records calls.
    Scripted items may be Exceptions (raised) or output-item lists."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs: Any):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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


def make_agent(config, tools: dict[str, Tool], script: list):
    client = StubClient(script)
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
    agent, client = make_agent(config, {}, [response([message_item("Done")])])
    events = await collect(agent, "hi")
    assert [e.type for e in events] == [EventType.THINKING, EventType.MODEL_RESPONSE]
    assert events[-1].quote == "Done"

    names = [type(m).__name__ for m in agent.context.messages]
    assert names == ["TextMessage", "TextMessage", "AssistantMessage"]

    sent = client.calls[0]["input"]
    assert sent[0] == {"role": "system", "content": "sys"}
    assert sent[1] == {"role": "user", "content": "hi"}
    assert client.calls[0]["store"] is False
    assert "include" not in client.calls[0]


async def test_text_reply_replayed_as_raw_message_item_across_turns(config):
    agent, client = make_agent(
        config,
        {},
        [response([message_item("first")]), response([message_item("second")])],
    )

    await collect(agent, "hi")
    await collect(agent, "again")

    replayed = client.calls[1]["input"]
    assert replayed[2] == {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": "first"}],
    }
    assert replayed[3] == {"role": "user", "content": "again"}


async def test_reasoning_summary_event_and_stateless_replay(config):
    config.reasoning_effort = "high"
    script = [
        response([reasoning_item("pondering"), function_call_item("1", "fake", {})]),
        response([message_item("done")]),
    ]
    agent, client = make_agent(
        config, {"fake": FakeTool(ToolResult(ok=True, content="r"))}, script
    )
    events = await collect(agent, "go")
    assert events[1].type is EventType.REASONING
    assert events[1].quote == "pondering"
    assert events[-1].type is EventType.MODEL_RESPONSE

    replayed = client.calls[1]["input"]
    assert replayed[2] == {  # reasoning item replayed verbatim
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "enc",
        "summary": [{"type": "summary_text", "text": "pondering"}],
    }
    assert replayed[3] == {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "1",
        "name": "fake",
        "arguments": {},
        "status": "completed",
    }
    assert replayed[4] == ToolMessage(call_id="1", content="r").serialize()
    assert any(isinstance(m, ReasoningMessage) for m in agent.context.messages)


async def test_reasoning_text_event_shown_when_summary_missing(config):
    config.reasoning_effort = "high"
    agent, _ = make_agent(
        config,
        {},
        [response([reasoning_item(text="thinking live"), message_item("ok")])],
    )

    events = await collect(agent, "hi")

    assert [e.type for e in events] == [
        EventType.THINKING,
        EventType.REASONING,
        EventType.MODEL_RESPONSE,
    ]
    assert events[1].quote == "thinking live"


async def test_reasoning_text_preferred_over_summary(config):
    config.reasoning_effort = "high"
    items = [reasoning_item(summary="recap", text="thinking live"), message_item("ok")]
    agent, _ = make_agent(config, {}, [response(items)])

    events = await collect(agent, "hi")

    assert [e.type for e in events] == [
        EventType.THINKING,
        EventType.REASONING,
        EventType.MODEL_RESPONSE,
    ]
    assert events[1].quote == "thinking live"
    assert events[2].quote == "ok"


async def test_reasoning_param_sent_only_when_effort_set(config):
    config.reasoning_effort = "high"
    agent, client = make_agent(config, {}, [response([message_item("ok")])])
    await collect(agent, "hi")
    assert client.calls[0]["reasoning"] == {"effort": "high", "summary": "auto"}
    assert client.calls[0]["include"] == ["reasoning.encrypted_content"]

    config.reasoning_effort = None
    agent, client = make_agent(config, {}, [response([message_item("ok")])])
    await collect(agent, "hi")
    assert "reasoning" not in client.calls[0]
    assert "include" not in client.calls[0]


async def test_reasoning_without_encrypted_content_shown_but_not_replayed(config):
    first = response(
        [reasoning_item("musing", encrypted=False), function_call_item("1", "fake", {})]
    )
    script = [first, response([message_item("done")])]
    agent, client = make_agent(
        config, {"fake": FakeTool(ToolResult(ok=True, content="r"))}, script
    )
    events = await collect(agent, "go")
    assert events[1].type is EventType.REASONING
    assert events[1].quote == "musing"

    replayed = client.calls[1]["input"]
    assert all(m.get("type") != "reasoning" for m in replayed)
    assert ToolMessage(call_id="1", content="r").serialize() in replayed


async def test_results_append_in_tool_call_order(config):
    tools = {
        "slow": FakeTool(ToolResult(ok=True, content="slow"), delay=0.05, name="slow"),
        "fast": FakeTool(ToolResult(ok=True, content="fast"), name="fast"),
    }
    batch = response(
        [
            function_call_item("1", "slow", {}),
            function_call_item("2", "fast", {}),
        ]
    )
    agent, _ = make_agent(config, tools, [batch, response([message_item("done")])])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.MODEL_RESPONSE

    tool_messages = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
    assert [m.call_id for m in tool_messages] == ["1", "2"]
    assert [m.content for m in tool_messages] == ["slow", "fast"]


async def test_duplicate_call_ids_do_not_collide(config):
    tools = {
        "one": FakeTool(ToolResult(ok=True, content="first"), name="one"),
        "two": FakeTool(ToolResult(ok=True, content="second"), name="two"),
    }
    batch = response(
        [
            function_call_item("1", "one", {}),
            function_call_item("1", "two", {}),  # same call_id
        ]
    )
    agent, _ = make_agent(config, tools, [batch, response([message_item("done")])])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.MODEL_RESPONSE

    tool_messages = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
    assert [m.call_id for m in tool_messages] == ["1", "1"]
    assert [m.content for m in tool_messages] == ["first", "second"]


async def test_declined_mutating_tool_recorded(config, fs):
    tools = {
        "write_file": WriteFile(config=config, fs=fs, prompt_user=awaitable_no()),
    }
    batch = response(
        [function_call_item("1", "write_file", {"path": "x.txt", "content": "d"})]
    )
    agent, _ = make_agent(config, tools, [batch, response([message_item("ok")])])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.MODEL_RESPONSE

    tool_messages = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
    assert "not approved" in tool_messages[0].content
    assert not (config.workspace / "x.txt").exists()


def awaitable_no():
    async def prompt_user(message: str, markdown: bool = True) -> str:
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
    agent, client = make_agent(config, {}, [response([message_item("nope")])])
    events = await collect(agent, "y" * 10_000)
    assert [e.type for e in events] == [EventType.ERROR]
    assert "token limit" in events[0].error_message
    assert client.calls == []


async def test_context_overflow_after_tools(config):
    config.max_context = 500
    tools = {"big": FakeTool(ToolResult(ok=True, content="x" * 100_000), name="big")}
    batch = response([function_call_item("1", "big", {})])
    agent, _ = make_agent(config, tools, [batch, response([message_item("never")])])
    events = await collect(agent, "go")
    assert events[-1].type is EventType.ERROR
    assert "token limit" in events[-1].error_message


async def test_max_iterations_reached(config):
    config.max_iterations = 2
    batch = response([function_call_item("1", "fake", {})])
    agent, _ = make_agent(
        config, {"fake": FakeTool(ToolResult(ok=True, content="r"))}, [batch] * 5
    )
    events = await collect(agent, "go")
    assert events[-1].type is EventType.ERROR
    assert "iteration limit" in events[-1].error_message


def test_truncate_tool_output_under_limit_untouched():
    assert _truncate_tool_output("short", 100) == "short"


def test_truncate_tool_output_cuts_on_line_boundary():
    text = "".join(f"line {i}\n" for i in range(1000))
    out = _truncate_tool_output(text, 40)
    assert out == (
        "".join(f"line {i}\n" for i in range(5))
        + "... [truncated after 5 lines, 8855 characters omitted; "
        "continue from the last line shown]"
    )


def test_truncate_tool_output_single_long_line_falls_back_to_char_cut():
    out = _truncate_tool_output("x" * 300, 100)
    assert out == "x" * 100 + "... [truncated, 200 characters omitted]"
