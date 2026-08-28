from __future__ import annotations

from imp.agent.context import Context, _estimate_context_tokens
from imp.entities import TextMessage


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
