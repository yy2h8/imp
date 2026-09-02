from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True, frozen=True)
class TextMessage:
    role: str
    content: str

    def serialize(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content}


@dataclass(slots=True, frozen=True)
class ToolCall:
    call_id: str
    function_name: str
    arguments: dict[str, Any]
    item: dict[str, Any]

    def serialize(self) -> dict[str, Any]:
        return self.item

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ToolCall:
        arguments = data.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Tool call {data.get('call_id')} returned invalid JSON arguments"
                ) from exc
        return ToolCall(
            call_id=data["call_id"],
            function_name=data["name"],
            arguments=arguments,
            item=data,
        )


@dataclass(slots=True, frozen=True)
class ReasoningMessage:
    """Reasoning item kept verbatim for stateless replay (encrypted content)."""

    item: dict[str, Any]
    content: str | None = None  # reasoning text or summary, for display

    def serialize(self) -> dict[str, Any]:
        return self.item

    @classmethod
    def parse(cls, data: dict[str, Any]) -> ReasoningMessage:
        text = "".join(
            part["text"]
            for part in data.get("content") or []
            if part.get("type") == "reasoning_text" and part.get("text")
        )
        summary = "\n".join(
            s["text"] for s in data.get("summary") or [] if s.get("text")
        )
        return cls(item=data, content=text or summary or None)


@dataclass(slots=True, frozen=True)
class AssistantMessage:
    content: str | None
    item: dict[str, Any]

    def serialize(self) -> dict[str, Any]:
        return self.item

    @classmethod
    def parse(cls, data: dict[str, Any]) -> AssistantMessage:
        text = "".join(
            part.get("text", "")
            for part in data.get("content") or []
            if part.get("type") == "output_text"
        )
        return cls(content=text or None, item=data)


@dataclass(slots=True, frozen=True)
class ToolMessage:
    call_id: str
    content: str

    def serialize(self) -> dict[str, Any]:
        return {
            "type": "function_call_output",
            "call_id": self.call_id,
            "output": self.content,
        }


ConversationMessage = (
    TextMessage | AssistantMessage | ReasoningMessage | ToolCall | ToolMessage
)
