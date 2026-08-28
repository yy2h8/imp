from __future__ import annotations

import json

from ..adapters import SessionWriter
from ..config import Config
from ..entities import ConversationMessage, TextMessage


def _token_formula(m: ConversationMessage) -> int:
    # TODO replace with tiktoken or other token counting method
    return len(json.dumps(m.serialize(), ensure_ascii=False, default=str)) // 4 + 20


def _estimate_context_tokens(messages: list[ConversationMessage]) -> int:
    return sum(_token_formula(m) for m in messages)


class Context:
    MAX_CONTEXT_TOKEN_BUFFER = 0.95

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
        if self.writer is not None:
            self.writer.write(message)

    def get_usage(self) -> tuple[int, int]:
        return self.tokens, self.config.max_context

    def is_within_token_limit(self) -> bool:
        used_tokens, max_tokens = self.get_usage()
        return used_tokens <= max_tokens * self.MAX_CONTEXT_TOKEN_BUFFER
