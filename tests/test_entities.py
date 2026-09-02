from __future__ import annotations

import pytest

from imp.entities import (
    AssistantMessage,
    ReasoningMessage,
    TextMessage,
    ToolCall,
    ToolMessage,
)


def function_call_item(
    arguments: object, call_id: str = "call_1", name: str = "list_dir"
):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


class TestToolCallParse:
    def test_dict_arguments_pass_through(self):
        call = ToolCall.parse(function_call_item({"path": "."}))
        assert call.arguments == {"path": "."}

    def test_json_string_arguments_parsed(self):
        call = ToolCall.parse(function_call_item('{"path": "."}'))
        assert call.arguments == {"path": "."}

    def test_empty_arguments_become_empty_dict(self):
        assert ToolCall.parse(function_call_item("")).arguments == {}

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="invalid JSON"):
            ToolCall.parse(function_call_item("{not json"))


class TestToolCallSerialize:
    def test_replays_item_verbatim(self):
        item = {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "id",
            "name": "read_file",
            "arguments": '{"a": 1}',
            "status": "completed",
        }
        call = ToolCall.parse(item)
        assert call.serialize() is item
        assert call.arguments == {"a": 1}


class TestReasoningMessage:
    def test_parse_prefers_text_over_summary_and_keeps_item_verbatim(self):
        item = {
            "type": "reasoning",
            "id": "rs_1",
            "encrypted_content": "enc",
            "content": [{"type": "reasoning_text", "text": "live"}],
            "summary": [
                {"type": "summary_text", "text": "a"},
                {"type": "summary_text", "text": "b"},
            ],
        }
        msg = ReasoningMessage.parse(item)
        assert msg.content == "live"
        assert msg.serialize() is item

    def test_parse_without_text_parts_falls_back_to_summary(self):
        item = {
            "type": "reasoning",
            "id": "rs_2",
            "summary": [{"type": "summary_text", "text": "recap"}],
        }
        assert ReasoningMessage.parse(item).content == "recap"

    def test_parse_without_text_or_summary_gives_none(self):
        msg = ReasoningMessage.parse({"type": "reasoning", "id": "rs_3"})
        assert msg.content is None

    def test_parse_joins_reasoning_text_parts(self):
        msg = ReasoningMessage.parse(
            {
                "type": "reasoning",
                "id": "rs_4",
                "content": [
                    {"type": "reasoning_text", "text": "a"},
                    {"type": "reasoning_text", "text": "b"},
                ],
            }
        )
        assert msg.content == "ab"


class TestAssistantMessage:
    def test_parse_keeps_item_verbatim_and_joins_output_text_parts(self):
        item = {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [
                {"type": "output_text", "text": "a"},
                {"type": "output_text", "text": "b"},
            ],
        }
        msg = AssistantMessage.parse(item)
        assert msg.content == "ab"
        assert msg.serialize() is item

    def test_parse_joins_output_text_parts(self):
        item = {
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "output_text", "text": "a"},
                {"type": "output_text", "text": "b"},
            ],
        }
        assert AssistantMessage.parse(item).content == "ab"

    def test_parse_without_text_parts_gives_none(self):
        item = {"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}
        assert AssistantMessage.parse(item).content is None


class TestSimpleMessages:
    def test_text_message(self):
        assert TextMessage(role="user", content="c").serialize() == {
            "role": "user",
            "content": "c",
        }

    def test_tool_message_serializes_as_function_call_output(self):
        assert ToolMessage(call_id="t", content="r").serialize() == {
            "type": "function_call_output",
            "call_id": "t",
            "output": "r",
        }
