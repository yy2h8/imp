from __future__ import annotations

import pytest

from imp.agent.context import (
    COMPACT_MARKER,
    COMPACT_TOOL_OUTPUT_CHARS,
    Context,
    _estimate_context_tokens,
)
from imp.entities import TextMessage, ToolCall, ToolMessage


def test_usage_tracks_messages(config):
    context = Context(config=config, system_prompt="sys")
    used, maximum = context.get_usage()
    assert maximum == config.max_context
    assert used == _estimate_context_tokens(context.messages)
    assert used > 0

    context.append(TextMessage(role="user", content="hello"))
    assert context.get_usage()[0] > used


def test_within_limit_when_small(config):
    context = Context(config=config, system_prompt="s")
    assert context.is_within_token_limit() is True


def test_exceeds_limit_above_buffer(config):
    config.max_context = 100
    context = Context(config=config, system_prompt="s")
    context.append(TextMessage(role="user", content="x" * 10_000))
    assert context.is_within_token_limit() is False


@pytest.mark.parametrize(
    "name, path, preserved",
    [
        ("read_file", ".imp/skills/example/SKILL.md", True),
        ("read_file", "skills/example/SKILL.md", True),
        ("read_file", "/workspace/skills/example/SKILL.md", True),
        ("read_file", "skills/example/SKILL.md.bak", False),
        ("read_file", "skills/example/SKILL.md/reference.md", False),
        ("read_file", None, False),
        ("read_file", 42, False),
        ("write_file", "skills/example/SKILL.md", False),
    ],
)
def test_compact_preserves_skill_reads_until_turn_eviction(config, name, path, preserved):
    context = Context(config, "sys")
    context.append(TextMessage(role="user", content="Load the skill"))
    content = "instructions\n" * COMPACT_TOOL_OUTPUT_CHARS
    for call_id, arguments in (
        ("page1", {"path": path, "end_line": 50}),
        ("page2", {"path": path, "start_line": 51}),
        ("ordinary", {"path": "notes.txt"}),
    ):
        context.append(
            ToolCall.parse(
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
        )
        context.append(ToolMessage(call_id=call_id, content=content))
    assert context.compact() == 0  # current turn is untouched
    context.append(TextMessage(role="user", content="Continue"))

    assert context.compact() > 0
    outputs = {
        m.call_id: m.content for m in context.messages if isinstance(m, ToolMessage)
    }
    for call_id in ("page1", "page2"):
        if preserved:
            assert outputs[call_id] == content
        else:
            assert COMPACT_MARKER in outputs[call_id]
    assert COMPACT_MARKER in outputs["ordinary"]
    assert context.tokens == _estimate_context_tokens(context.messages)
    snapshot = [m.serialize() for m in context.messages]
    assert context.compact() == 0
    assert [m.serialize() for m in context.messages] == snapshot

    config.max_context = 2 * _estimate_context_tokens(
        [context.messages[0], context.messages[-1]]
    )
    assert context.trim() == 7
    assert context.messages == [
        TextMessage(role="system", content="sys"),
        TextMessage(role="user", content="Continue"),
    ]
    assert context.tokens == _estimate_context_tokens(context.messages)
    assert context.is_within_token_limit()
