from __future__ import annotations

from types import SimpleNamespace

import pytest

from imp.entities import (
    AssistantMessage,
    TextMessage,
    ToolCall,
    ToolMessage,
)


def sdk_tool_call(arguments: object, call_id: str = "call_1", name: str = "list_dir"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class TestToolCallParse:
    def test_dict_arguments_pass_through(self):
        call = ToolCall.parse(sdk_tool_call({"path": "."}))
        assert call.arguments == {"path": "."}

    def test_json_string_arguments_parsed(self):
        call = ToolCall.parse(sdk_tool_call('{"path": "."}'))
        assert call.arguments == {"path": "."}

    def test_empty_arguments_become_empty_dict(self):
        assert ToolCall.parse(sdk_tool_call("")).arguments == {}

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            ToolCall.parse(sdk_tool_call("{not json"))


class TestToolCallSerialize:
    def test_shape(self):
        call = ToolCall(
            tool_call_id="id", function_name="read_file", arguments={"a": 1}
        )
        data = call.serialize()
        assert data["id"] == "id"
        assert data["type"] == "function"
        assert data["function"]["name"] == "read_file"
        assert data["function"]["arguments"] == '{"a": 1}'


class TestAssistantMessage:
    def test_parse_plain_content(self):
        msg = AssistantMessage.parse(SimpleNamespace(content="hi", tool_calls=None))
        assert msg.content == "hi"
        assert msg.tool_calls is None

    def test_parse_with_tool_calls(self):
        msg = AssistantMessage.parse(
            SimpleNamespace(content=None, tool_calls=[sdk_tool_call({"x": 1})])
        )
        assert msg.tool_calls is not None
        assert len(msg.tool_calls) == 1

    def test_serialize_omits_tool_calls_when_absent(self):
        assert "tool_calls" not in AssistantMessage(content="hi").serialize()

    def test_serialize_includes_tool_calls(self):
        call = ToolCall(tool_call_id="id", function_name="f", arguments={})
        payload = AssistantMessage(content=None, tool_calls=[call]).serialize()
        assert payload["tool_calls"][0]["id"] == "id"


class TestSimpleMessages:
    def test_text_message(self):
        assert TextMessage(role="user", content="c").serialize() == {
            "role": "user",
            "content": "c",
        }

    def test_tool_message(self):
        assert ToolMessage(tool_call_id="t", content="r").serialize() == {
            "role": "tool",
            "tool_call_id": "t",
            "content": "r",
        }
