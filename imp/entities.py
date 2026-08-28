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
    tool_call_id: str
    function_name: str
    arguments: dict[str, Any]

    def serialize(self) -> dict[str, Any]:
        return {
            "id": self.tool_call_id,
            "type": "function",
            "function": {
                "name": self.function_name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }

    @classmethod
    def parse(cls, data: Any) -> ToolCall:
        arguments = data.function.arguments or {}
        if not isinstance(arguments, dict):
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Tool call {data.id} returned invalid JSON arguments"
                ) from exc
        else:
            parsed_arguments = arguments

        return ToolCall(
            tool_call_id=data.id,
            function_name=data.function.name,
            arguments=parsed_arguments,
        )


@dataclass(slots=True, frozen=True)
class AssistantMessage:
    content: str | None
    tool_calls: list[ToolCall] | None = None

    def serialize(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = [
                tool_call.serialize() for tool_call in self.tool_calls
            ]
        return payload

    @classmethod
    def parse(cls, data: Any) -> AssistantMessage:
        tool_calls = [
            ToolCall.parse(tool_call)
            for tool_call in getattr(data, "tool_calls", None) or []
        ]
        return AssistantMessage(
            content=getattr(data, "content", None),
            tool_calls=tool_calls if tool_calls else None,
        )


@dataclass(slots=True, frozen=True)
class ToolMessage:
    tool_call_id: str
    content: str

    def serialize(self) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }


ConversationMessage = TextMessage | AssistantMessage | ToolMessage
