from __future__ import annotations

import asyncio
import tracemalloc
import weakref
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from imp.agent import Agent, EventType
from imp.agent.context import COMPACT_TOOL_OUTPUT_CHARS, Context, _token_formula
from imp.agent.executor import _truncate_tool_output, execute_tool_batch
from imp.entities import (
    AssistantMessage,
    ReasoningMessage,
    TextMessage,
    ToolCall,
    ToolMessage,
)
from imp.events import Usage
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


def response(items: list, usage=None):
    return SimpleNamespace(output=items, usage=usage)


def usage_ns(input: int = 0, output: int = 0, total: int = 0, cost=None):
    """Stand-in for the SDK usage block; cost is OpenRouter-specific and may
    be absent entirely (built dynamically only when provided)."""
    attrs = {
        "input_tokens": input,
        "output_tokens": output,
        "total_tokens": total,
    }
    if cost is not None:
        attrs["cost"] = cost
    return SimpleNamespace(**attrs)


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


async def test_cancelling_turn_joins_read_tools(config):
    started = asyncio.Event()
    stopped = asyncio.Event()
    tasks = []

    class WaitingTool(FakeTool):
        async def execute(self, **kwargs):
            tasks.append(asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    tool = WaitingTool(ToolResult(ok=True, content="unused"))
    agent, _ = make_agent(
        config,
        {tool.name: tool},
        [response([function_call_item("wait", tool.name, {})])],
    )
    turn = asyncio.create_task(collect(agent, "wait"))
    await asyncio.wait_for(started.wait(), 1)
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)
    try:
        assert stopped.is_set()
        assert all(task.done() for task in tasks)
        outputs = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
        assert len(outputs) == 1
        assert "interrupt" in outputs[0].content.lower()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_model_response_event_carries_usage(config):
    reply = response([message_item("ok")], usage=usage_ns(10, 5, 15, cost=0.0123))
    agent, _ = make_agent(config, {}, [reply])
    events = await collect(agent, "hi")
    assert events[-1].type is EventType.MODEL_RESPONSE
    assert events[-1].usage == Usage(10, 5, 15, 0.0123)


async def test_model_response_event_without_usage(config):
    reply = response([message_item("ok")], usage=None)
    agent, _ = make_agent(config, {}, [reply])
    events = await collect(agent, "hi")
    assert events[-1].type is EventType.MODEL_RESPONSE
    assert events[-1].usage is None


async def test_usage_cost_absent_yields_none(config):
    reply = response([message_item("ok")], usage=usage_ns(10, 5, 15))  # no cost attr
    agent, _ = make_agent(config, {}, [reply])
    events = await collect(agent, "hi")
    assert events[-1].usage == Usage(10, 5, 15, None)


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


@pytest.mark.parametrize("mutating", [False, True])
async def test_tool_batch_releases_consumed_oversized_results(config, mutating):
    config.max_tool_output = 10
    raw_refs = []
    retained_tasks = []  # Python 3.12's as_completed retains a completed task.
    waiting = asyncio.Event()
    consumed = asyncio.Event()
    release = asyncio.Event()

    class TrackedText(str):
        pass

    class ProducingTool(FakeTool):
        async def execute(self, **kwargs):
            retained_tasks.append(asyncio.current_task())
            content = TrackedText("x" * 10_000)
            diff = TrackedText("y" * 10_000)
            raw_refs.extend((weakref.ref(content), weakref.ref(diff)))
            return ToolResult(ok=True, content=content, diff=diff)

    class WaitingTool(FakeTool):
        async def execute(self, **kwargs):
            waiting.set()
            await release.wait()
            return ToolResult(ok=True, content="done")

    producing = ProducingTool(ToolResult(ok=True, content="unused"))
    waiting_tool = WaitingTool(ToolResult(ok=True, content="unused"))
    producing.mutating = waiting_tool.mutating = mutating
    context = Context(config=config, system_prompt="sys")
    calls = [ToolCall(str(i), "produce", {}, {}) for i in range(8)]
    calls.append(ToolCall("wait", "wait", {}, {}))
    batch = execute_tool_batch(
        {"produce": producing, "wait": waiting_tool}, calls, context
    )

    async def consume():
        count = 0
        async for event in batch:
            if event.tool_result is not None and event.tool_name == "produce":
                assert event.tool_result.content == "x" * 10_000
                assert event.tool_result.diff == "y" * 10_000
                count += 1
                if count == len(calls) - 1:
                    consumed.set()
            del event

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), 1)
        await asyncio.wait_for(consumed.wait(), 1)
        assert len(raw_refs) == 2 * (len(calls) - 1)
        assert all(ref() is None for ref in raw_refs)
    finally:
        release.set()
        await consumer

    outputs = [m for m in context.messages if isinstance(m, ToolMessage)]
    assert [m.call_id for m in outputs] == [call.call_id for call in calls]
    assert [m.content for m in outputs] == [
        "xxxxxxxxxx... [truncated, 9990 characters omitted]"
    ] * (len(calls) - 1) + ["done"]


async def test_tool_batch_overlaps_reads_before_sequential_writes(config):
    second_read_started = asyncio.Event()
    completed = []

    class ReadTool(FakeTool):
        async def execute(self, **kwargs):
            if self.name == "read_one":
                await second_read_started.wait()
            else:
                second_read_started.set()
            completed.append(self.name)
            return ToolResult(ok=True, content=self.name)

    class WriteTool(FakeTool):
        mutating = True

        async def execute(self, **kwargs):
            # Yield so a concurrent write would finish before this one.
            if self.name == "write_one":
                await asyncio.sleep(0)
            completed.append(self.name)
            return ToolResult(ok=True, content=self.name)

    tools = {
        name: cls(ToolResult(ok=True, content="unused"), name=name)
        for cls, name in [
            (WriteTool, "write_one"),
            (ReadTool, "read_one"),
            (WriteTool, "write_two"),
            (ReadTool, "read_two"),
        ]
    }
    calls = [ToolCall(name, name, {}, {}) for name in tools]
    context = Context(config=config, system_prompt="sys")

    async def consume():
        async for _ in execute_tool_batch(tools, calls, context):
            pass

    await asyncio.wait_for(consume(), 1)
    assert completed == ["read_two", "read_one", "write_one", "write_two"]
    outputs = [m for m in context.messages if isinstance(m, ToolMessage)]
    assert [m.call_id for m in outputs] == list(tools)


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
    assert events[-1].type is EventType.ERROR
    assert not any(isinstance(m, ToolMessage) for m in agent.context.messages)


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
    agent, _ = make_agent(
        config,
        {"fake": FakeTool(ToolResult(ok=True, content="r"))},
        [response([function_call_item(str(i), "fake", {})]) for i in range(5)],
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


def test_truncate_tool_output_allocates_only_for_retained_prefix():
    text = "line\n" * 100_000
    tracemalloc.start()
    try:
        output = _truncate_tool_output(text, 100)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert output.startswith("line\n" * 20)
    assert peak < len(text)


@pytest.mark.parametrize(
    "separator",
    ["\n", "\r\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_truncate_tool_output_preserves_splitlines_boundaries(separator):
    first = "a" + separator
    text = first + "bcdef"
    assert _truncate_tool_output(text, len(first)) == (
        first + "... [truncated after 1 lines, 5 characters omitted; "
        "continue from the last line shown]"
    )
    assert _truncate_tool_output(text, len(first) - 1) == (
        text[: len(first) - 1] + "... [truncated, 6 characters omitted]"
    )


async def test_duplicate_calls_rejected_before_execution(config):
    tool = FakeTool(ToolResult(ok=True, content="effect"))
    agent, _ = make_agent(
        config,
        {"fake": tool},
        [
            response(
                [
                    function_call_item("same", "fake", {}),
                    function_call_item("same", "fake", {}),
                ]
            )
        ],
    )
    events = await collect(agent, "go")
    assert any(event.type is EventType.ERROR for event in events)
    assert not any(event.type is EventType.TOOL_START for event in events)


async def test_multiple_text_and_refusal_are_visible(config):
    refusal = sdk_item(
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "refusal", "refusal": "Cannot do that"}],
        }
    )
    agent, _ = make_agent(config, {}, [response([message_item("First"), refusal])])
    events = await collect(agent, "go")
    assert events[-1].quote == "First\nCannot do that"


async def test_malformed_later_call_prevents_entire_batch(config):
    tool = FakeTool(ToolResult(ok=True, content="side effect"))
    agent, _ = make_agent(
        config,
        {"fake": tool},
        [
            response(
                [
                    function_call_item("good", "fake", {}),
                    function_call_item("bad", "fake", "[1]"),
                ]
            )
        ],
    )
    events = await collect(agent, "go")
    assert events[-1].type is EventType.ERROR
    assert not any(event.type is EventType.TOOL_START for event in events)
    assert not any(
        type(message).__name__ == "ToolCall" for message in agent.context.messages
    )


async def test_incomplete_response_never_executes_tools(config):
    reply = response([function_call_item("one", "fake", {})])
    reply.status = "incomplete"
    reply.incomplete_details = {"reason": "max_output_tokens"}
    agent, _ = make_agent(
        config, {"fake": FakeTool(ToolResult(ok=True, content="effect"))}, [reply]
    )
    events = await collect(agent, "go")
    assert events[-1].type is EventType.ERROR
    assert "max_output_tokens" in events[-1].error_message
    assert not any(event.type is EventType.TOOL_START for event in events)


async def test_consumer_close_preserves_completed_result_and_next_turn(config):
    agent, client = make_agent(
        config,
        {"fake": FakeTool(ToolResult(ok=True, content="real result"))},
        [
            response([function_call_item("one", "fake", {})]),
            response([message_item("next answer")]),
        ],
    )
    stream = agent.run_turn("first")
    async for event in stream:
        if event.type is EventType.TOOL_RESULT:
            break
    await stream.aclose()
    outputs = [m for m in agent.context.messages if isinstance(m, ToolMessage)]
    assert [m.content for m in outputs] == ["real result"]
    events = await collect(agent, "second")
    assert events[-1].quote == "next answer"
    assert any(
        item.get("type") == "function_call_output" for item in client.calls[-1]["input"]
    )


class RecordingWriter:
    """SessionWriter stand-in: records every persisted message."""

    def __init__(self) -> None:
        self.rows: list = []

    def write(self, message) -> None:
        self.rows.append(message)


def test_token_formula_weights_non_ascii():
    # Equal-length prose: Russian costs measurably more o200k tokens than
    # English (272 vs 161 for these ~800-char samples).
    english = TextMessage(
        role="user",
        content="The quick brown fox jumps over the lazy dog near the riverbank at dawn. "
        * 10,
    )
    russian = TextMessage(
        role="user",
        content="Быстрая бурая лиса прыгает через ленивую собаку возле берега реки на рассвете. "
        * 10,
    )
    assert _token_formula(russian) > _token_formula(english)




def test_replace_system_prompt_writes_once(config):
    writer = RecordingWriter()
    context = Context(config, "one", writer=writer)
    context.replace_system_prompt("two")
    assert len(writer.rows) == 1


def test_append_skips_contentless_reasoning(config):
    writer = RecordingWriter()
    context = Context(config, "sys", writer=writer)
    context.append(
        ReasoningMessage(
            item={"type": "reasoning", "id": "rs_1", "summary": []}, content=None
        )
    )
    assert len(writer.rows) == 1  # only the system row from __init__
    context.append(
        ReasoningMessage(
            item={
                "type": "reasoning",
                "id": "rs_2",
                "summary": [],
                "encrypted_content": "blob",
            },
            content=None,
        )
    )
    assert len(writer.rows) == 2
    context.append(
        ReasoningMessage(
            item={
                "type": "reasoning",
                "id": "rs_3",
                "summary": [{"type": "summary_text", "text": "hmm"}],
            },
            content="hmm",
        )
    )
    assert len(writer.rows) == 3


def seeded_compact_context(config) -> Context:
    """System + one completed prior turn + one in-flight current turn."""
    context = Context(config, "sys")
    context.append(TextMessage(role="user", content="first question"))
    context.append(
        ReasoningMessage(
            item={
                "type": "reasoning",
                "id": "rs_old",
                "summary": [],
                "encrypted_content": "old-blob",
            },
            content=None,
        )
    )
    context.append(
        ToolCall(
            call_id="c1",
            function_name="fake",
            arguments={},
            item={
                "type": "function_call",
                "call_id": "c1",
                "name": "fake",
                "arguments": "{}",
            },
        )
    )
    context.append(ToolMessage(call_id="c1", content="x" * 5_000))
    context.append(
        AssistantMessage(
            content="first answer",
            item={
                "type": "message",
                "role": "assistant",
                "id": "msg_1",
                "content": [{"type": "output_text", "text": "first answer"}],
            },
        )
    )
    context.append(TextMessage(role="user", content="second question"))
    context.append(
        ReasoningMessage(
            item={
                "type": "reasoning",
                "id": "rs_new",
                "summary": [],
                "encrypted_content": "new-blob",
            },
            content=None,
        )
    )
    context.append(
        ToolCall(
            call_id="c2",
            function_name="fake",
            arguments={},
            item={
                "type": "function_call",
                "call_id": "c2",
                "name": "fake",
                "arguments": "{}",
            },
        )
    )
    context.append(ToolMessage(call_id="c2", content="y" * 5_000))
    return context


def test_compact_stubs_prior_turn_only(config):
    context = seeded_compact_context(config)
    before = context.tokens
    assert context.compact() > 0
    assert context.tokens < before
    old_reasoning = context.messages[2]
    assert isinstance(old_reasoning, ReasoningMessage)
    assert "encrypted_content" not in old_reasoning.item
    new_reasoning = context.messages[7]
    assert isinstance(new_reasoning, ReasoningMessage)
    assert "encrypted_content" in new_reasoning.item
    old_output = context.messages[4]
    assert isinstance(old_output, ToolMessage)
    assert old_output.content.startswith("x" * COMPACT_TOOL_OUTPUT_CHARS)
    assert "older tool output compacted" in old_output.content
    new_output = context.messages[9]
    assert isinstance(new_output, ToolMessage)
    assert new_output.content == "y" * 5_000
    serialized = [m.serialize() for m in context.messages]
    calls = [s for s in serialized if s.get("type") == "function_call"]
    outputs = [s for s in serialized if s.get("type") == "function_call_output"]
    assert len(calls) == len(outputs) == 2  # call/output pairing stays balanced
    snapshot = [m.serialize() for m in context.messages]
    assert context.compact() == 0
    assert [m.serialize() for m in context.messages] == snapshot  # idempotent


def test_trim_drops_oldest_whole_turns(config):
    config.max_context = 5_000  # two ~4k-token turns overflow; one fits
    context = Context(config, "sys")
    for label in ("one", "two"):
        content = " ".join(f"{label}{i}" for i in range(1_000))
        context.append(TextMessage(role="user", content=content))
        context.append(
            AssistantMessage(
                content=content,
                item={
                    "type": "message",
                    "role": "assistant",
                    "id": f"msg_{label}",
                    "content": [{"type": "output_text", "text": content}],
                },
            )
        )
    context.append(TextMessage(role="user", content="hi"))
    assert context.trim() == 2
    assert [m.serialize().get("role") for m in context.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert context.messages[1].content.startswith("two0 two1")
    assert context.messages[3].content == "hi"
    assert context.is_within_token_limit()


async def test_context_warning_emitted_once_when_approaching(config):
    config.max_context = 14_000
    config.max_tool_output = 100_000
    tools = {"fake": FakeTool(ToolResult(ok=True, content="x" * 100_000))}
    script = [
        response([function_call_item("one", "fake", {})]),
        response([message_item("done")]),
    ]
    agent, _client = make_agent(config, tools, script)
    events = await collect(agent, "go")
    warnings = [e for e in events if e.type is EventType.CONTEXT_WARNING]
    assert len(warnings) == 1
    assert warnings[0].token_usage[0] > config.max_context * 0.85




async def test_agent_recovers_over_limit_after_tools(config):
    config.max_context = 20_000
    config.max_tool_output = 100_000
    tools = {"fake": FakeTool(ToolResult(ok=True, content="x" * 100_000))}
    script = [
        response([function_call_item("one", "fake", {})]),
        response([message_item("done")]),
        response([function_call_item("two", "fake", {})]),
        response([message_item("recovered")]),
    ]
    agent, client = make_agent(config, tools, script)
    await collect(agent, "first")
    events = await collect(agent, "second")
    assert not any(event.type is EventType.ERROR for event in events)
    assert events[-1].quote == "recovered"
    assert len(client.calls) == 4
    assert any(event.type is EventType.CONTEXT_COMPACTED for event in events)
    stubbed = [
        m
        for m in agent.context.messages
        if isinstance(m, ToolMessage) and "older tool output compacted" in m.content
    ]
    assert [m.call_id for m in stubbed] == ["one"]


async def test_agent_trims_fat_text_turns_when_compaction_insufficient(config):
    config.max_context = 15_000
    config.max_tool_output = 100_000
    fat_prompt = "first " + " ".join(f"p{i}" for i in range(3_000))  # ~6k tokens
    tools = {"fake": FakeTool(ToolResult(ok=True, content="x" * 100_000))}
    script = [
        response([message_item("done")]),  # fat turn stays text-only
        response([function_call_item("two", "fake", {})]),
        response([message_item("recovered")]),
    ]
    agent, _client = make_agent(config, tools, script)
    await collect(agent, fat_prompt)
    events = await collect(agent, "second")
    assert not any(event.type is EventType.ERROR for event in events)
    assert events[-1].quote == "recovered"
    assert any(event.type is EventType.CONTEXT_TRIMMED for event in events)
    assert not any(
        isinstance(m, TextMessage) and m.content == fat_prompt
        for m in agent.context.messages
    )
