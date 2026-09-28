from __future__ import annotations

from assistant.adapters.telegram import MAX_MESSAGE_CHARS
from assistant.adapters.ui import (
    StatusBuffer,
    TelegramUIAdapter,
    split,
)
from imp.events import AgentEvent, EventType
from imp.tools.base import ToolResult


def ToolResult_ok(content: str) -> ToolResult:
    return ToolResult(ok=True, content=content)


def test_split_preserves_arbitrary_content():
    text = "# title\n```\n" + "😀_*" * 5000 + "\n```\n\n"
    chunks = split(text)
    assert "".join(chunks) == text
    assert all(
        len(chunk.encode("utf-16-le")) // 2 <= MAX_MESSAGE_CHARS for chunk in chunks
    )


class TestStatusBuffer:
    def test_render_joins_lines(self):
        buffer = StatusBuffer(max_chars=100)
        buffer.append("a")
        buffer.append("b")
        assert buffer.render() == "a\nb"

    def test_oldest_lines_dropped_at_cap(self):
        buffer = StatusBuffer(max_chars=10)
        buffer.append("12345")
        buffer.append("67890")
        buffer.append("abc")
        assert buffer.render() == "67890\nabc"  # "12345" dropped

    def test_giant_single_line_tail_with_ellipsis(self):
        buffer = StatusBuffer(max_chars=10)
        buffer.append("x" * 50)
        assert buffer.render() == "…" + "x" * 9

    def test_collapse_replaces_with_summary(self):
        buffer = StatusBuffer(max_chars=100)
        buffer.append("tool stuff")
        buffer.collapse("✓ done · 2 tools · 5 s")
        assert buffer.render() == "✓ done · 2 tools · 5 s"

    def test_collapse_truncates_to_cap(self):
        buffer = StatusBuffer(max_chars=5)
        buffer.collapse("way too long summary")
        assert buffer.render() == "way t"


class FakeBot:
    """Records calls; optionally refuses edits."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.edit_fails = False
        self.next_id = 100

    async def send_message(self, chat_id: int, text: str):
        self.sent.append((chat_id, text))
        self.next_id += 1
        return self.next_id

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return not self.edit_fails

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        self.actions = getattr(self, "actions", [])
        self.actions.append(action)


class TestTelegramUIAdapter:
    async def test_status_created_once_and_edited_after_debounce(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, edit_interval=3600, max_chars=500)
        await ui.handle(
            AgentEvent(
                type=EventType.TOOL_START,
                token_usage=(0, 100),
                tool_name="run_shell",
                tool_args={"command": "ls"},
            )
        )
        await ui.flush()  # first flush creates the message regardless of debounce
        assert len(bot.sent) == 1
        message_id = bot.sent[0] and ui.status_message_id
        await ui.handle(
            AgentEvent(
                type=EventType.TOOL_RESULT,
                token_usage=(0, 100),
                tool_name="run_shell",
                tool_result=ToolResult_ok("out"),
            )
        )
        await ui.flush()  # within the debounce window: skipped
        assert len(bot.edits) == 0
        await ui.flush(force=True)  # forced: edits in place
        assert len(bot.sent) == 1  # no second message
        assert bot.edits[-1][1] == message_id

    async def test_final_answer_is_separate_message(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, edit_interval=3600, max_chars=500)
        await ui.handle(
            AgentEvent(
                type=EventType.MODEL_RESPONSE,
                token_usage=(0, 100),
                quote="thinking out loud",
            )
        )
        await ui.flush()
        await ui.end_turn("✓ done · 0 tools · 1 s")
        await ui.answer("Here is **the answer**.")
        assert bot.sent[0][1].startswith("💭")  # status line for the thought
        assert bot.sent[-1][1] == "Here is **the answer**."  # separate message
        assert len(bot.sent) == 2
        assert bot.edits == [(1, ui.status_message_id, "✓ done · 0 tools · 1 s")]

    async def test_answer_splits_long_text(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1)
        await ui.answer("word " * 3000)
        assert len(bot.sent) > 1
        assert all(len(text) <= MAX_MESSAGE_CHARS for _, text in bot.sent)

    async def test_persistent_edit_failure_falls_back_to_fresh_message(self):

        bot = FakeBot()
        bot.edit_fails = True
        ui = TelegramUIAdapter(bot, chat_id=1, edit_interval=0.0, max_chars=500)
        await ui.handle(
            AgentEvent(
                type=EventType.TOOL_START,
                token_usage=(0, 100),
                tool_name="list_dir",
                tool_args={},
            )
        )
        await ui.flush()
        await ui.handle(
            AgentEvent(
                type=EventType.TOOL_RESULT,
                token_usage=(0, 100),
                tool_name="list_dir",
                tool_result=ToolResult_ok("[]"),
            )
        )
        await ui.flush(force=True)  # edit fails → fresh message, editing stops
        assert len(bot.sent) == 2
        assert ui.status_message_id == bot.next_id

    async def test_begin_creates_status_message_eagerly(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1)
        await ui.begin()
        assert bot.sent == [(1, "…")]  # visible from the first second
        assert ui.status_message_id is not None
        await ui.begin()  # idempotent: no second status message
        assert bot.sent == [(1, "…")]

    async def test_error_renders_bold_line(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.handle(
            AgentEvent(
                type=EventType.ERROR,
                token_usage=(0, 100),
                error_message="model exploded",
            )
        )
        assert "*error:* model exploded" in ui.buffer.render()
