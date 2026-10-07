from __future__ import annotations

import json
from dataclasses import replace

import tiktoken

from ..adapters import SessionWriter
from ..config import Config
from ..entities import (
    ConversationMessage,
    ReasoningMessage,
    TextMessage,
    ToolMessage,
)

# Older tool outputs are stubbed to this length once their turn completes.
COMPACT_TOOL_OUTPUT_CHARS = 500
COMPACT_MARKER = "older tool output compacted"

_ENCODING: tiktoken.Encoding | None = None


def _encoding() -> tiktoken.Encoding:
    """o200k BPE, the closest widely-available vocabulary to the models in
    use; loaded once. Exact for OpenAI models, a good approximation for
    others — only thresholds depend on the count."""
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("o200k_base")
    return _ENCODING


def _token_formula(m: ConversationMessage) -> int:
    # Counts the exact serialized item that goes into the request; unlike a
    # chars/4 estimate this prices Cyrillic/CJK text correctly.
    text = json.dumps(m.serialize(), ensure_ascii=False, default=str)
    return len(_encoding().encode(text))


def _estimate_context_tokens(messages: list[ConversationMessage]) -> int:
    return sum(_token_formula(m) for m in messages)


class Context:
    MAX_CONTEXT_TOKEN_BUFFER = 0.95
    CONTEXT_WARNING_BUFFER = 0.85

    def __init__(
        self,
        config: Config,
        system_prompt: str,
        writer: SessionWriter | None = None,
    ) -> None:
        self.config = config
        message = TextMessage(role="system", content=system_prompt)
        self.messages = [message]
        self.tokens = _estimate_context_tokens(self.messages)
        self.writer = writer
        if writer is not None:
            writer.write(message)

    def append(self, message: ConversationMessage) -> None:
        self.messages.append(message)
        self.tokens += _token_formula(message)
        # Contentless reasoning rows (no encrypted payload, no summary) are
        # never replayed and carry no display text — skip persisting them.
        if self.writer is not None and not (
            isinstance(message, ReasoningMessage)
            and "encrypted_content" not in message.item
            and not message.item.get("summary")
        ):
            self.writer.write(message)

    def replace_system_prompt(self, content: str) -> None:
        self.messages[0] = TextMessage(role="system", content=content)
        self.tokens = _estimate_context_tokens(self.messages)

    def _current_turn_start(self) -> int:
        for i in range(len(self.messages) - 1, 0, -1):
            m = self.messages[i]
            if isinstance(m, TextMessage) and m.role == "user":
                return i
        return 1

    def compact(self) -> int:
        """Shrink replay cost of completed prior turns: drop their encrypted
        reasoning and stub oversized old tool outputs. Call/output pairing and
        the current turn are never touched; idempotent. Full outputs remain in
        the persisted session transcript. Returns tokens saved."""
        start = self._current_turn_start()
        for i in range(1, start):
            m = self.messages[i]
            if isinstance(m, ReasoningMessage) and "encrypted_content" in m.item:
                item = {k: v for k, v in m.item.items() if k != "encrypted_content"}
                self.messages[i] = replace(m, item=item)
            elif (
                isinstance(m, ToolMessage)
                and len(m.content) > COMPACT_TOOL_OUTPUT_CHARS
                and COMPACT_MARKER not in m.content
            ):
                kept = m.content[:COMPACT_TOOL_OUTPUT_CHARS]
                omitted = len(m.content) - len(kept)
                self.messages[i] = replace(
                    m,
                    content=kept + f"\n... [{COMPACT_MARKER}, {omitted} chars omitted]",
                )
        before = self.tokens
        self.tokens = _estimate_context_tokens(self.messages)
        return before - self.tokens


    def trim(self) -> int:
        """Drop oldest completed turns (whole turns only) until within the
        context limit. Keeps the system message and the current turn.
        Returns messages dropped."""
        dropped = 0
        while not self.is_within_token_limit():
            boundary = next(
                (
                    i
                    for i in range(2, len(self.messages))
                    if isinstance(self.messages[i], TextMessage)
                    and self.messages[i].role == "user"
                ),
                None,
            )
            # boundary is the second turn's start: dropping everything
            # before it can never eat the current turn (boundary <= current)
            if boundary is None:
                return dropped
            dropped += boundary - 1
            del self.messages[1:boundary]
            self.tokens = _estimate_context_tokens(self.messages)
        return dropped

    def get_usage(self) -> tuple[int, int]:
        return self.tokens, self.config.max_context

    def is_within_token_limit(self) -> bool:
        used_tokens, max_tokens = self.get_usage()
        return used_tokens <= max_tokens * self.MAX_CONTEXT_TOKEN_BUFFER

    def is_approaching_limit(self) -> bool:
        used_tokens, max_tokens = self.get_usage()
        return used_tokens > max_tokens * self.CONTEXT_WARNING_BUFFER
