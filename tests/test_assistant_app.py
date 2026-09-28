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
from assistant.bootstrap import read_state, write_state
from assistant.config import AssistantConfig
from assistant.main import PollLoop, TurnRunner, turn_summary
from assistant.uploads import Uploads
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

    async def send_text(self, chat_id: int, text: str) -> list[int]:
        self.sent.append(text)
        return [len(self.sent)]

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
    instance = AssistantApp(
        config=imp_config,
        assistant=assistant_config,
        agent=agent,
        session=session,
        bot=None,
        chat_id=7,
        ask_router=AskRouter(),
        uploads=Uploads(bot=None, inbox=home / "inbox"),
    )

    try:
        yield instance
    finally:
        instance.session.writer.__exit__(None, None, None)


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

    # exactly two outbound messages: the eager status message and the answer
    assert len(bot.sent) == 2
    assert bot.sent[0] == "…"  # created at turn start, before any event
    assert "🔧 `fake` x=1" in bot.edits[0]  # tool start rendered into it
    assert bot.edits[-1] == "✓ done · 1 tools · 0 s"  # collapsed one-liner
    # the final answer is its own message, sent after the collapse
    assert bot.sent[1] == "All **done**."
    # the typing indicator was refreshed while the turn ran
    assert bot.chat_actions.count("typing") >= 1


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

    # events: TOOL_START, TOOL_RESULT, MODEL_RESPONSE — with a 60 s debounce
    # only the forced end_turn collapse is ever edited (the eager "…" message
    # is created directly, never edited before that)
    assert len(bot.edits) == 1
    assert bot.edits[0].startswith("✓ done ·")


async def test_command_new_resets_session(app, home):
    bot = FakeBot()
    old_transcript = app.session.writer.path
    runner = TurnRunner(app, bot, edit_interval=0.0, status_max_chars=3500)

    await runner.run("/new")

    assert bot.sent == [
        f"Started a fresh session. Previous transcript is saved. New transcript: `{app.session.writer.path.name}`"
    ]
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
        "message": {
            "from": {"id": user_id},
            "chat": {"id": user_id, "type": "private"},
            "text": text,
        },
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
            [
                owner_update("first", update_id=100),
                owner_update("stranger", user_id=99),
            ],
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

    app.agent.tools = {
        "ask": build_tools(
            config=app.config, fs=None, prompt_user=prompt_user, http=None
        )["ask"]
    }

    loop = PollLoop(app, bot)
    await loop._handle_update(owner_update("start"))  # turn starts as a task
    await asyncio.sleep(0.05)  # let the turn reach the ask tool
    await loop._handle_update(
        owner_update("the answer", update_id=101)
    )  # mid-turn: routes to ask
    await loop.turn_task

    assert any("which one?" in text for text in bot.sent)  # the question went out
    tool_messages = [
        m for m in app.agent.context.messages if type(m).__name__ == "ToolMessage"
    ]
    assert any("the answer" in m.content for m in tool_messages)
    assert "got it" in bot.sent[-1]


async def test_messages_before_a_question_stay_queued(app):
    bot = FakeBot()
    loop = PollLoop(app, bot)
    started = asyncio.Event()
    ask_now = asyncio.Event()
    asked = asyncio.Event()
    prompts = []
    answers = []

    async def run_turn(prompt):
        prompts.append(prompt)
        if prompt == "start":
            started.set()
            await ask_now.wait()
            future = app.ask_router.start()
            asked.set()
            answers.append(await future)
            app.ask_router.clear()

    loop._run_turn = run_turn
    await loop._handle_update(owner_update("start"))
    await asyncio.wait_for(started.wait(), 2)
    try:
        await loop._handle_update(owner_update("next task", update_id=101))
        ask_now.set()
        await asyncio.wait_for(asked.wait(), 2)
        assert answers == []
        await loop._handle_update(owner_update("actual answer", update_id=102))
        await asyncio.wait_for(loop.turn_task, 2)
        assert answers == ["actual answer"]
        assert prompts == ["start", "next task"]
    finally:
        loop.turn_task.cancel()
        await asyncio.gather(loop.turn_task, return_exceptions=True)


async def test_waiting_requests_survive_restart_in_fifo_order(app, home):
    bot = UpdateBot([])
    loop = PollLoop(app, bot)
    started = asyncio.Event()

    async def blocked_turn(prompt):
        started.set()
        await asyncio.Event().wait()

    loop._run_turn = blocked_turn
    await loop._handle_update(owner_update("running", update_id=100))
    await asyncio.wait_for(started.wait(), 2)
    try:
        await loop._handle_update(owner_update("first waiting", update_id=101))
        await loop._handle_update(owner_update("second waiting", update_id=102))
        state = read_state(home)
        assert state["offset"] == 103
        assert state["active_request"] == "running"
        assert state["pending_requests"] == ["first waiting", "second waiting"]
    finally:
        loop.turn_task.cancel()
        await asyncio.gather(loop.turn_task, return_exceptions=True)

    # A new loop reads disk. It must not execute the interrupted request.
    resumed = PollLoop(app, bot)
    completed = asyncio.Event()
    prompts = []

    async def record_turn(prompt):
        prompts.append(prompt)
        if len(prompts) == 2:
            completed.set()

    resumed._run_turn = record_turn
    task = asyncio.create_task(resumed.poll_forever())
    try:
        await asyncio.wait_for(completed.wait(), 2)
        await asyncio.wait_for(resumed.turn_task, 2)
        assert prompts == ["first waiting", "second waiting"]
        assert any("interrupted" in message.lower() for message in bot.sent)
        assert read_state(home)["active_request"] is None
        assert read_state(home)["pending_requests"] == []
        assert bot.polls[0] == 103
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_duplicate_update_does_not_repeat_work(app, home):
    bot = FakeBot()
    loop = PollLoop(app, bot)
    prompts = []

    async def record_turn(prompt):
        prompts.append(prompt)

    loop._run_turn = record_turn
    update = owner_update("once", update_id=100)
    await loop._handle_update(update)
    await loop.turn_task
    await loop._handle_update(update)
    await loop.turn_task
    assert prompts == ["once"]
    assert read_state(home)["offset"] == 101


async def test_failed_queue_write_does_not_acknowledge_or_run(app, home, monkeypatch):
    write_state(home, {"offset": 100, "pending_requests": []})
    loop = PollLoop(app, FakeBot())

    def fail_write(home, updates):
        raise OSError("disk full")

    monkeypatch.setattr("assistant.main.write_state", fail_write)
    with pytest.raises(OSError, match="disk full"):
        await loop._handle_update(owner_update("do not run", update_id=100))
    assert loop.offset == 100
    assert loop.turn_task is None
    assert read_state(home)["offset"] == 100
    assert read_state(home)["pending_requests"] == []


async def test_worker_drains_without_waiting_for_long_poll(app, home):
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    polling = asyncio.Event()

    class BlockingPollBot(FakeBot):
        async def get_updates(self, offset):
            polling.set()
            await asyncio.Event().wait()

    write_state(home, {"pending_requests": ["first", "second"]})
    loop = PollLoop(app, BlockingPollBot())
    prompts = []

    async def run_turn(prompt):
        prompts.append(prompt)
        if prompt == "first":
            started.set()
            await release.wait()
        else:
            finished.set()

    loop._run_turn = run_turn
    task = asyncio.create_task(loop.poll_forever())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(polling.wait(), 2)
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
        assert prompts == ["first", "second"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert loop.turn_task.done()


# ------------------------------------------------------------------- uploads


class UploadBot(FakeBot):
    """FakeBot plus getFile/download_file feeding bytes from a dict."""

    def __init__(self, files: dict[str, bytes] | None = None):
        super().__init__()
        self.files = files or {}

    async def get_file(self, file_id: str) -> dict:
        return {"file_path": f"docs/{file_id}"}

    async def download_file(self, file_path: str) -> bytes:
        return self.files[file_path.removeprefix("docs/")]


def document_update(file_id: str, name: str, caption: str | None = None) -> dict:
    message: dict = {
        "from": {"id": 7},
        "chat": {"id": 7, "type": "private"},
        "document": {"file_id": file_id, "file_name": name, "file_size": 5},
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": 200, "message": message}


def with_upload_bot(app, bot):
    """Point the app's upload handler at the test's transport."""
    app.uploads.bot = bot
    app.uploads.chat_id = app.chat_id
    return app


async def test_upload_without_caption_saves_and_acks_only(app, home):
    bot = UpdateBot([])
    bot.__class__ = UploadBot
    bot.files = {"f1": b"hello"}
    with_upload_bot(app, bot)
    script_client(app, [response([message_item("should not run")])])

    loop = PollLoop(app, bot)
    await loop._handle_update(document_update("f1", "note.txt"))
    await loop.turn_task

    assert (home / "inbox" / "note.txt").read_bytes() == b"hello"
    assert any("Saved inbox/note.txt" in text for text in bot.sent)
    assert len(app.agent.context.messages) == 1  # system only: no turn started


async def test_captioned_upload_starts_a_turn(app, home):
    bot = UpdateBot([[owner_update("unused")], []])
    bot.__class__ = UploadBot
    bot.files = {"f1": b"data"}
    with_upload_bot(app, bot)
    script_client(app, [response([message_item("here is the summary")])])

    loop = PollLoop(app, bot)
    await loop._handle_update(document_update("f1", "doc.txt", caption="read it"))
    await loop.turn_task

    turn_prompt = app.agent.client.calls[0]["input"][1]
    assert "inbox/doc.txt" in turn_prompt["content"]
    assert "read it" in turn_prompt["content"]


async def test_captioned_upload_mid_turn_is_held_not_answer(app, home):
    """A captioned upload during a pending ask must not resolve the ask."""
    bot = UpdateBot([])
    bot.__class__ = UploadBot
    bot.files = {"f1": b"data"}
    with_upload_bot(app, bot)

    async def prompt_user(message: str, markdown: bool = True) -> str:
        future = app.ask_router.start()
        await bot.send_message(app.chat_id, message)
        return await future

    from imp.tools import build_tools

    app.agent.tools = {
        "ask": build_tools(
            config=app.config, fs=None, prompt_user=prompt_user, http=None
        )["ask"]
    }

    loop = PollLoop(app, bot)
    ask_future = app.ask_router.start()  # simulate the ask tool waiting
    loop.turn_task = asyncio.create_task(asyncio.sleep(3600))  # turn "running"
    await asyncio.sleep(0)

    await loop._handle_update(document_update("f1", "mid.txt", caption="look"))

    assert not ask_future.done()  # the upload never resolved the ask
    assert read_state(home)["pending_requests"] == [
        {
            "attachment": {
                "document": {"file_id": "f1", "file_name": "mid.txt", "file_size": 5},
                "caption": "look",
            }
        }
    ]
    app.ask_router.clear()
    later_question = app.ask_router.start()
    assert not later_question.done()
    loop.turn_task.cancel()
    await asyncio.gather(loop.turn_task, return_exceptions=True)


async def test_group_message_cannot_start_work(app):
    loop = PollLoop(app, FakeBot())
    update = owner_update("run")
    update["message"]["chat"] = {"id": -42, "type": "group"}
    await loop._handle_update(update)
    assert loop.turn_task is None
    assert loop.pending == []


def test_command_prefix_is_not_a_command(app):
    runner = TurnRunner(app, FakeBot(), 0, 3500)
    assert runner._command_reply("/newsletter") is None


async def test_upload_intake_does_not_download_while_asking(app):
    loop = PollLoop(app, FakeBot())
    future = app.ask_router.start()
    loop.turn_task = asyncio.create_task(asyncio.Event().wait())

    async def forbidden(message):
        raise AssertionError("polling must not download")

    app.uploads.handle = forbidden
    try:
        await loop._handle_update(document_update("file", "file.txt"))
        await loop._handle_update(owner_update("answer", update_id=201))
        assert future.result() == "answer"
        assert len(loop.pending) == 1
    finally:
        loop.turn_task.cancel()
        await asyncio.gather(loop.turn_task, return_exceptions=True)


async def test_execution_lock_keeps_waiting_request_durable(app, home):
    loop = PollLoop(app, FakeBot())
    async with app.execution_lock:
        await loop._handle_update(owner_update("waiting"))
        await asyncio.sleep(0)
        assert read_state(home)["pending_requests"] == ["waiting"]
        assert read_state(home).get("active_request") is None
        loop.turn_task.cancel()
        await asyncio.gather(loop.turn_task, return_exceptions=True)


async def test_failed_recovery_notice_preserves_active_marker(app, home):
    from assistant.adapters.telegram import TelegramError

    class FailedBot(FakeBot):
        async def send_message(self, *args):
            return None

        async def send_text(self, *args):
            return None

    write_state(
        home, {"active_request": "previous work", "pending_requests": ["waiting"]}
    )
    with pytest.raises(TelegramError):
        await PollLoop(app, FailedBot()).poll_forever()
    assert read_state(home)["active_request"] == "previous work"
    assert read_state(home)["pending_requests"] == ["waiting"]


async def test_empty_final_does_not_repeat_commentary(app):
    bot = FakeBot()
    script_client(
        app,
        [
            response([message_item("Working"), function_call_item("one", "fake", {})]),
            response([]),
        ],
    )
    await TurnRunner(app, bot, 0, 3500).run("go")
    assert "Working" not in bot.sent


@pytest.mark.parametrize(
    "attachment",
    [
        {"photo": []},
        {"document": []},
        {"voice": {}},
        {"document": {"file_id": 4}},
        {"document": {"file_id": "ok"}, "caption": []},
    ],
)
def test_malformed_attachment_state_is_rejected(app, home, attachment):
    write_state(home, {"pending_requests": [{"attachment": attachment}]})
    with pytest.raises(ValueError, match="queue"):
        PollLoop(app, FakeBot())


async def test_corrupt_queue_stops_before_startup(tmp_path, monkeypatch):
    import assistant.main as main_module

    config = AssistantConfig(
        bot_token="t", allowed_user_ids=frozenset({7}), home=tmp_path
    )
    monkeypatch.setattr(main_module.AssistantConfig, "from_env", lambda: config)
    write_state(tmp_path, {"pending_requests": [{"attachment": {"photo": []}}]})

    async def forbidden(*args, **kwargs):
        raise AssertionError("tailoring must not start")

    monkeypatch.setattr(main_module, "startup", forbidden)
    with pytest.raises(ValueError, match="queue"):
        await main_module.run_bot()


async def test_answer_cannot_jump_to_next_question_during_save(app, monkeypatch):
    loop = PollLoop(app, FakeBot())
    loop.turn_task = asyncio.create_task(asyncio.Event().wait())
    app.ask_router.start()
    original = loop._save
    next_question = []

    async def save_and_change_question(**updates):
        await original(**updates)
        if not next_question:
            app.ask_router.clear()
            next_question.append(app.ask_router.start())

    monkeypatch.setattr(loop, "_save", save_and_change_question)
    try:
        await loop._handle_update(owner_update("for first question"))
        assert not next_question[0].done()
        assert loop.pending == ["for first question"]
    finally:
        app.ask_router.clear()
        loop.turn_task.cancel()
        await asyncio.gather(loop.turn_task, return_exceptions=True)
