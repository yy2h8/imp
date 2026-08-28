from __future__ import annotations

from typing import Any, ClassVar

from imp.agent.prompt import build_system_prompt
from imp.tools import Tool, ToolResult


class FakeTool(Tool):
    name = "fake"
    description = "a fake tool"
    parameters: ClassVar[dict[str, Any]] = {}

    async def execute(self, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=True, content="")


class InstructedTool(FakeTool):
    name = "instructed"
    instructions = "Always do X."


def build(tools=None, skills=(), context=""):
    return build_system_prompt("ws", ["a.py"], tools or {}, list(skills), context)


def test_environment_section():
    prompt = build()
    assert "## Environment" in prompt
    assert "ws" in prompt
    assert "a.py" in prompt


def test_tool_sections():
    prompt = build(tools={"fake": FakeTool()})
    assert "## Available Tools" in prompt
    assert "**fake** - a fake tool" in prompt
    assert "## Tool Instructions" not in prompt


def test_tool_instructions_section():
    prompt = build(tools={"instructed": InstructedTool()})
    assert "## Tool Instructions" in prompt
    assert "Always do X." in prompt


def test_skills_section():
    prompt = build(skills=[("solo",), ("named", "with description")])
    assert "## Available Skills" in prompt
    assert "- solo" in prompt
    assert "- **named** - with description" in prompt


def test_no_skills_section_when_empty():
    assert "## Available Skills" not in build()


def test_project_context_section():
    assert "## Project Instructions" in build(context="PROJECT NOTES")
    assert "PROJECT NOTES" in build(context="PROJECT NOTES")
    assert "## Project Instructions" not in build()
