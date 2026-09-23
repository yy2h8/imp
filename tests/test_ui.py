from __future__ import annotations

from assistant.adapters.ui import (
    MAX_MESSAGE_CHARS,
    StatusBuffer,
    TelegramUIAdapter,
    sanitise,
    split,
)
from imp.events import AgentEvent, EventType
from imp.tools.base import ToolResult


def ToolResult_ok(content: str) -> ToolResult:
    return ToolResult(ok=True, content=content)


class TestSanitise:
    def test_heading_becomes_bold_and_escaped(self):
        out = sanitise("# Title *with* _fancy_ chars")
        assert out == "*Title \\*with\\* \\_fancy\\_ chars*"

    def test_table_becomes_fenced_rows(self):
        text = "before\n| a | b |\n|---|---|\n| 1 | 2 |\nafter"
        assert sanitise(text).splitlines() == [
            "before",
            "```",
            "a · b",
            "1 · 2",
            "```",
            "after",
        ]

    def test_table_separator_row_dropped(self):
        assert "|---|---|" not in sanitise("| a | b |\n|---|---|\n| 1 | 2 |")

    def test_nested_list_markers_flattened(self):
        text = "- one\n  - two\n    - three\n1. first"
        assert sanitise(text).splitlines() == ["- one", "- two", "- three", "- first"]

    def test_table_at_end_closes_fence(self):
        out = sanitise("| a |\n| 1 |")
        assert out.endswith("```")

    def test_plain_text_passthrough(self):
        assert sanitise("just text\nwith lines") == "just text\nwith lines"


class TestSplit:
    def test_short_text_single_chunk(self):
        assert split("hello") == ["hello"]
        assert split("") == []

    def test_split_at_limit(self):
        text = "a" * 10 + "\n" + "b" * 10
        chunks = split(text, limit=12)
        assert chunks == ["a" * 10 + "\n", "b" * 10]

    oversize = None  # placeholder to keep the class flat

    def test_long_single_line_is_hard_cut(self):
        text = "x" * 25
        chunks = split(text, limit=10)
        assert "".join(chunks).startswith("x")
        assert all(len(c) <= 12 for c in chunks)  # fence lines can exceed limit

    def test_fence_cut_closed_and_reopened(self):
        text = "```\n" + "a\nb\nc\n" + "```\n" + "after"
        # cut inside the fence: chunk1 ends with close, chunk2 reopens
        chunks = split(text, limit=12)
        assert chunks[0].endswith("```\n")
        assert chunks[1].startswith("```\n")
        assert "".join(chunks).count("```") % 2 == 0  # fence parity restored

    def test_no_open_fence_across_chunks(self):
        text = ("```\n" + "x" * 30 + "\n```\n") * 3
        chunks = split(text, limit=20)
        for chunk in chunks:
            assert chunk.count("```") % 2 == 0

    def test_max_message_chars_constant(self):
        assert MAX_MESSAGE_CHARS == 4096


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
            AgentEvent(type=EventType.TOOL_START, token_usage=(0, 100),
                       tool_name="run_shell", tool_args={"command": "ls"})
        )
        await ui.flush()  # first flush creates the message regardless of debounce
        assert len(bot.sent) == 1
        message_id = bot.sent[0] and ui.status_message_id
        await ui.handle(
            AgentEvent(type=EventType.TOOL_RESULT, token_usage=(0, 100),
                       tool_name="run_shell",
                       tool_result=ToolResult_ok("out"))
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
            AgentEvent(type=EventType.MODEL_RESPONSE, token_usage=(0, 100),
                       quote="thinking out loud")
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
            AgentEvent(type=EventType.TOOL_START, token_usage=(0, 100),
                       tool_name="list_dir", tool_args={})
        )
        await ui.flush()
        await ui.handle(
            AgentEvent(type=EventType.TOOL_RESULT, token_usage=(0, 100),
                       tool_name="list_dir", tool_result=ToolResult_ok("[]"))
        )
        await ui.flush(force=True)  # edit fails → fresh message, editing stops
        assert len(bot.sent) == 2
        assert ui.status_message_id == bot.next_id

    async def test_thinking_sends_typing_action(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1)
        await ui.handle(AgentEvent(type=EventType.THINKING, token_usage=(0, 100)))
        assert bot.actions == ["typing"]

    async def test_error_renders_bold_line(self):

        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.handle(
            AgentEvent(type=EventType.ERROR, token_usage=(0, 100),
                       error_message="model exploded")
        )
        assert "*error:* model exploded" in ui.buffer.render()
