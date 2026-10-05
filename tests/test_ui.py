"""Turn status rendering: thinking header, tool-only live log, cost summary."""

from __future__ import annotations

from assistant.adapters.markdown import MAX_TEXT_CHARS
from assistant.adapters.telegram import MAX_MESSAGE_CHARS, split
from assistant.adapters.ui import (
    STATUS_LINES,
    StatusBuffer,
    TelegramUIAdapter,
    tool_label,
    tool_subject,
    turn_summary,
)
from imp.events import AgentEvent, EventType


def event(**kwargs) -> AgentEvent:
    kwargs.setdefault("type", EventType.TOOL_START)
    kwargs.setdefault("token_usage", (0, 100))
    return AgentEvent(**kwargs)


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

    def test_line_cap_marks_overflow(self):
        buffer = StatusBuffer(max_chars=1000)
        for i in range(12):
            buffer.append(f"line {i}")
        rendered = buffer.render()
        assert rendered.startswith("… +4 earlier\n")
        assert "line 11" in rendered and "line 4" in rendered
        assert "line 3" not in rendered  # only STATUS_LINES kept


class TestTurnSummary:
    def test_success_with_cost(self):
        assert turn_summary(3, 47, True, 0.0134) == "✓ done · 3 tools · 47 s · $0.0134"

    def test_success_without_cost(self):
        assert turn_summary(1, 2, True, None) == "✓ done · 1 tools · 2 s"

    def test_failure(self):
        assert turn_summary(3, 12, False, None) == "✗ failed · 3 tools · 12 s"
        assert turn_summary(3, 12, False, 0.5) == "✗ failed · 3 tools · 12 s"


class TestToolSubject:
    def test_path_tools_take_path(self):
        assert tool_subject("read_file", {"path": "imp/agent/model.py"}) == (
            "imp/agent/model.py"
        )

    def test_long_path_falls_back_to_basename(self):
        long = "assistant/deeply/nested/directory/tree/that/keeps/going/model.py"
        assert len(long) > 40
        assert tool_subject("read_file", {"path": long}) == "model.py"

    def test_run_shell_takes_first_command_line(self):
        subject = tool_subject(
            "run_shell", {"command": "ruff check imp/\n# a long tail\nmore"}
        )
        assert subject == "ruff check imp/"

    def test_run_shell_command_truncated(self):
        assert tool_subject("run_shell", {"command": "x" * 80}) == "x" * 40

    def test_web_subjects(self):
        assert tool_subject("web_fetch", {"url": "https://example.com/a"}) == (
            "example.com"
        )
        assert tool_subject("web_search", {"query": "q" * 80}) == "q" * 40

    def test_misc_subjects(self):
        assert tool_subject("send_file", {"path": "out/report.txt"}) == "report.txt"
        assert tool_subject("schedule_job", {"id": "daily"}) == "daily"
        assert tool_subject("ask", {"question": "Proceed?\nmore"}) == "Proceed?"
        assert tool_subject("memory_set", {"key": "server"}) == "server"
        assert tool_subject("cost_report", {"period": "week"}) == "week"
        assert tool_subject("unknown_tool", None) == ""



class TestToolLabel:
    def test_short_name_plus_subject(self):
        label = tool_label("run_shell", {"command": "tar -czf x.tgz dir"})
        assert label == "shell tar -czf x.tgz dir"

    def test_unknown_tool_uses_truncated_name(self):
        assert tool_label("very_long_tool_name", None) == "very_lon"

    def test_none_name_is_empty(self):
        assert tool_label(None, None) == ""


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

    async def send_text(self, chat_id: int, text: str):
        self.sent.append((chat_id, text))
        self.next_id += 1
        return [self.next_id]

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return not self.edit_fails

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass


def tool_event(name: str, args: dict | None = None) -> AgentEvent:
    return event(
        type=EventType.TOOL_START, tool_name=name, tool_args=args or {}
    )


class TestTelegramUIAdapter:
    async def test_begin_sends_thinking_header(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1)
        await ui.begin()
        assert bot.sent == [(1, "🧠 thinking…")]
        assert ui.status_message_id is not None
        await ui.begin()  # idempotent
        assert len(bot.sent) == 1

    async def test_tool_line_in_mono_block_with_working_header(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.begin()
        await ui.handle(tool_event("read_file", {"path": "imp/agent/model.py"}))
        await ui.flush(force=True)
        text = bot.edits[-1][2]
        assert text.startswith("🧠 working\n```")
        assert "read" in text and "imp/agent/model.py" in text
        assert text.rstrip().endswith("```")

    async def test_reasoning_and_model_response_change_nothing(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.begin()
        await ui.handle(event(type=EventType.REASONING, quote="deep thought"))
        await ui.handle(event(type=EventType.MODEL_RESPONSE, quote="aloud"))
        await ui.handle(event(type=EventType.THINKING))
        await ui.flush(force=True)
        assert bot.edits == []  # nothing dirty: status stays at thinking

    async def test_tool_overflow_keeps_last_eight(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=4000)
        await ui.begin()
        for i in range(12):
            await ui.handle(tool_event("run_shell", {"command": f"cmd {i}"}))
        await ui.flush(force=True)
        text = bot.edits[-1][2]
        assert "… +4 earlier" in text
        assert f"cmd {11}" in text
        assert "cmd 3" not in text

    async def test_error_line_rendered(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.begin()
        await ui.handle(event(type=EventType.ERROR, error_message="model exploded"))
        await ui.flush(force=True)
        assert "*error:* model exploded" in bot.edits[-1][2]

    async def test_end_turn_collapses_to_summary(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.begin()
        await ui.handle(tool_event("read_file", {"path": "a.py"}))
        await ui.end_turn("✓ done · 1 tools · 3 s")
        assert bot.edits[-1][2] == "✓ done · 1 tools · 3 s"

    async def test_persistent_edit_failure_falls_back_to_fresh_message(self):
        bot = FakeBot()
        bot.edit_fails = True
        ui = TelegramUIAdapter(bot, chat_id=1, edit_interval=0.0, max_chars=500)
        await ui.handle(tool_event("list_dir", {"path": "."}))
        await ui.flush()
        await ui.handle(tool_event("read_file", {"path": "x"}))
        await ui.flush(force=True)  # edit fails → fresh message, editing stops
        assert len(bot.sent) == 2
        assert ui.status_message_id == bot.next_id

    async def test_throttle_skips_then_forced_edits(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, edit_interval=3600, max_chars=500)
        await ui.handle(tool_event("run_shell", {"command": "ls"}))
        await ui.flush()  # first flush creates regardless of debounce
        assert len(bot.sent) == 1
        message_id = ui.status_message_id
        await ui.handle(tool_event("read_file", {"path": "x"}))
        await ui.flush()  # within debounce window: skipped
        assert len(bot.edits) == 0
        await ui.flush(force=True)
        assert len(bot.sent) == 1  # still one message
        assert bot.edits[-1][1] == message_id

    async def test_answer_is_separate_message(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=500)
        await ui.begin()
        await ui.end_turn("✓ done · 0 tools · 1 s")
        await ui.answer("Here is **the answer**.")
        assert bot.sent[-1][1] == "Here is **the answer**."
        assert bot.sent[0][1] == "🧠 thinking…"

    async def test_status_text_fits_one_message(self):
        bot = FakeBot()
        ui = TelegramUIAdapter(bot, chat_id=1, max_chars=MAX_TEXT_CHARS)
        await ui.begin()
        for i in range(STATUS_LINES + 4):
            await ui.handle(
                tool_event("read_file", {"path": f"some/deep/dir/file{i}.py"})
            )
        await ui.flush(force=True)
        assert len(bot.edits[-1][2].encode("utf-16-le")) // 2 <= MAX_MESSAGE_CHARS
