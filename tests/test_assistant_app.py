"""Integration tests: a fake Telegram transport drives real turns through
TurnRunner and PollLoop (spec §11), asserting the status message is debounced
and edited (not re-sent per event) and the final answer is a separate message."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from assistant.app import (
    RESET_NOTICE,
    AskRouter,
    AssistantApp,
    Session,
    ensure_home,
)
from assistant.cli import PollLoop, TurnRunner, turn_summary
from assistant.config import AssistantConfig
from imp.agent import Agent
from imp.config import Config
from imp.tools.base import Tool, ToolResult


class FakeBot:
    """In-memory transport: records every call; edits can be forced to fail."""

    def __init__(self, fail_edits_after: int | None = None):
        self.sent: list[str] = []  # every outbound message text, in order
        self.edits: list[str] = []
        self.chat_actions: list[str] = []
        self.fail_edits_after = fail_edits_after
        self._edit_ok = 0

    async def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append(text)
        return len(self.sent)  # message_id

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        self._edit_ok += 1
        if self.fail_edits_after is not None and self._edit_ok > self.fail_edits_after:
            return False
        self.edits.append(text)
        return True

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.chat_actions.append(action)


class FakeTool(Tool):
    name = "fake"
    description = "fake tool"
    parameters: ClassVar[dict[str, Any]] = {}

    def __init__(self, result: ToolResult):
        self.result = result

    async def execute(self, **kwargs) -> ToolResult:
        return self.result


def sdk_item(data: dict):
    from types import SimpleNamespace

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
    from types import SimpleNamespace

    return SimpleNamespace(output=items)


class StubClient:
    """Plays scripted responses; scripted Exceptions are raised (like
    tests/test_agent.py's StubClient, minus the recording differences)."""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []
        self.responses = type("R", (), {"create": self._create})()

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def home(tmp_path: Path) -> Path:
    ensure_home(tmp_path)
    return tmp_path


@pytest.fixture
def app(home: Path) -> AssistantApp:
    imp_config = Config(api_key="k", workspace=home)
    assistant_config = AssistantConfig(
        bot_token="t", allowed_user_ids=frozenset({7}), home=home
    )
    session = Session.open(imp_config, "system prompt")
    agent = Agent(
        config=imp_config,
        tools={
            "fake": FakeTool(ToolResult(ok=True, content="tool did the thing")),
        },
        client=StubClient([]),
        context=session.context,
    )
    return AssistantApp(
        config=imp_config,
        assistant=assistant_config,
        agent=agent,
        session=session,
        bot=None,
        chat_id=7,
        ask_router=AskRouter(),
    )


def script_client(app: AssistantApp, script: list) -> StubClient:
    client = StubClient(script)
    app.agent.client = client
    return client


# ---------------------------------------------------------------- TurnRunner


async def test_full_turn_single_status_message_then_separate_answer(app, home):
    bot = FakeBot()
    script_client(
        app,
        [
            response([function_call_item("1", "fake", {"x": 1})]),
            response([message_item("All **done**.")]),
        ],
    )
    runner = TurnRunner(app, bot, edit_interval=0.0, status_max_chars=3500)

    await runner.run("do the thing")

    # exactly two outbound messages: the one status message and the answer
    assert len(bot.sent) == 2
    status = bot.sent[0]
    assert "🔧 `fake` x=1" in status  # tool start rendered into the status
    assert bot.edits[-1] == "✓ done · 1 tools · 0 s"  # collapsed one-liner
    # the final answer is its own message, sent after the collapse
    assert bot.sent[1] == "All **done**."


async def test_debounce_skips_edits_until_interval_passes(app, home):
    bot = FakeBot()
    script_client(
        app,
        [
            response([function_call_item("1", "fake", {})]),
            response([message_item("done")]),
        ],
    )
    runner = TurnRunner(app, bot, edit_interval=60.0, status_max_chars=3500)

    await runner.run("go")

    # events: THINKING, TOOL_START, TOOL_RESULT, MODEL_RESPONSE — but with a
    # 60 s debounce only the forced end_turn collapse is ever edited
    assert len(bot.edits) == 1
    assert bot.edits[0].startswith("✓ done ·")


async def test_command_new_resets_session(app, home):
    bot = FakeBot()
    old_transcript = app.session.writer.path
    runner = TurnRunner(app, bot, edit_interval=0.0, status_max_chars=3500)

    await runner.run("/new")

    assert bot.sent == [f"{RESET_NOTICE} New transcript: `{app.session.writer.path.name}`"]
    assert app.session.writer.path != old_transcript
    assert [m.role for m in app.agent.context.messages] == ["system"]


async def test_command_status_reports_usage_and_transcript(app, home):
    bot = FakeBot()
    runner = TurnRunner(app, bot, edit_interval=0.0, status_max_chars=3500)

    await runner.run("/status")

    used, maximum = app.usage
    expected = (
        f"*status:* {used}/{maximum} tokens ({used / maximum:.0%}) · "
        f"transcript `{app.session.writer.path.name}`"
    )
    assert bot.sent == [expected]


async def test_pre_turn_overflow_sends_reset_notice(app, home):
    bot = FakeBot()
    script_client(app, [response([message_item("fresh answer")])])
    # usage crosses the threshold (95%+ of budget) → reset, then answer
    app.assistant.reset_threshold = 0.0
    runner = TurnRunner(app, bot, edit_interval=0.0, status_max_chars=3500)

    await runner.run("hello")

    assert bot.sent[0].startswith(RESET_NOTICE)
    # the turn continued and answered after the reset (fresh context is empty)
    assert bot.sent[-1] == "fresh answer"


async def test_model_error_messages_owner_and_keeps_summary_failed(app, home):
    bot = FakeBot()
    script_client(app, [RuntimeError("model exploded")])
    runner = TurnRunner(app, bot, edit_interval=0.0, status_max_chars=3500)

    await runner.run("hello")  # must not raise

    assert any(text.startswith("*error:*") for text in bot.sent)
    assert "model exploded" in bot.sent[-1]
    assert bot.edits[-1] == "✗ failed"


async def test_turn_summary_line():
    assert turn_summary(3, 0.0).startswith("✓ done · 3 tools · ")
    assert turn_summary(0, 0.0, ok=False).startswith("✗ done · 0 tools · ")


# ------------------------------------------------------------------- PollLoop


class UpdateBot(FakeBot):
    """FakeBot plus scripted getUpdates pages for the poll loop."""

    def __init__(self, pages: list[list[dict]]):
        super().__init__()
        self.pages = list(pages)
        self.polls: list[int] = []

    async def get_updates(self, offset: int) -> list[dict]:
        self.polls.append(offset)
        await asyncio.sleep(0)  # yield, like the real network long-poll
        return self.pages.pop(0) if self.pages else []


def owner_update(text: str, update_id: int = 100, user_id: int = 7) -> dict:
    return {
        "update_id": update_id,
        "message": {"from": {"id": user_id}, "text": text},
    }


async def run_poll_loop(bot, app, cycles: int) -> PollLoop:
    """Drive poll_forever as a real task for `cycles` scripted pages."""
    loop = PollLoop(app, bot)
    task = asyncio.create_task(loop.poll_forever())
    for _ in range(cycles):
        await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return loop


async def test_poll_loop_persists_offset_and_filters_non_owner(app, home):
    bot = UpdateBot(
        [
            [owner_update("first", update_id=100), owner_update("stranger", user_id=99)],
            [],
            [],
        ]
    )
    script_client(app, [response([message_item("answer one")])])

    loop = await run_poll_loop(bot, app, cycles=2)

    assert loop.offset == 101  # update_id + 1
    state = json.loads((home / "state.json").read_text())
    assert state["offset"] == 101
    # only the owner's message became a turn
    assert bot.sent[0] != "stranger"


async def test_poll_loop_delivers_mid_turn_message_to_pending_ask(app, home):
    bot = UpdateBot([[owner_update("start"), owner_update("the answer")], []])
    client = StubClient(
        [
            response([function_call_item("1", "ask", {"question": "which one?"})]),
            response([message_item("got it")]),
        ]
    )
    app.agent.client = client

    async def prompt_user(message: str, markdown: bool = True) -> str:
        future = app.ask_router.start()
        await bot.send_message(app.chat_id, message)
        return await future

    from imp.tools import build_tools

    app.agent.tools = {"ask": build_tools(
        config=app.config, fs=None, prompt_user=prompt_user, http=None
    )["ask"]}

    loop = PollLoop(app, bot)
    await loop._handle_update(owner_update("start"))  # turn starts as a task
    await asyncio.sleep(0.05)  # let the turn reach the ask tool
    await loop._handle_update(owner_update("the answer"))  # mid-turn: routes to ask
    await loop.turn_task

    assert any("which one?" in text for text in bot.sent)  # the question went out
    tool_messages = [
        m for m in app.agent.context.messages if type(m).__name__ == "ToolMessage"
    ]
    assert any("the answer" in m.content for m in tool_messages)
    assert "got it" in bot.sent[-1]


async def test_poll_loop_queues_message_racing_a_pending_ask(app, home):
    """A message that lands between the turn starting and the ask tool
    starting must not be lost and must not deadlock the turn (§4.1)."""
    bot = FakeBot()

    class SlowThenAskClient(StubClient):
        async def _create(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                await asyncio.sleep(0.2)  # the ask has not started yet
                return response([function_call_item("1", "ask", {"question": "q?"})])
            return response([message_item("done")])

    client = SlowThenAskClient([])

    async def prompt_user(message: str, markdown: bool = True) -> str:
        future = app.ask_router.start()
        await bot.send_message(app.chat_id, message)
        return await future

    from imp.tools import build_tools

    app.agent.client = client
    app.agent.tools = {"ask": build_tools(
        config=app.config, fs=None, prompt_user=prompt_user, http=None
    )["ask"]}

    loop = PollLoop(app, bot)
    await loop._handle_update(owner_update("start"))
    await asyncio.sleep(0.05)  # mid first (slow) model call; no ask pending yet
    await loop._handle_update(owner_update("early reply"))  # must be held
    await loop.turn_task

    # the held message resolved the ask once it started
    tool_messages = [
        m for m in app.agent.context.messages if type(m).__name__ == "ToolMessage"
    ]
    assert any("early reply" in m.content for m in tool_messages)
    assert "done" in bot.sent[-1]
