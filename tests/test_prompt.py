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


def test_base_prompt_kwarg_replaces_imp_prompt():
    """The assistant seam (spec §3.1): a different base prompt, same layout."""
    prompt = build_system_prompt(
        "ws", [], {}, [], "", base_prompt="You are a general assistant."
    )
    assert prompt.startswith("You are a general assistant.")
    assert "# imp" not in prompt  # imp's own base prompt is gone
    assert "## Environment" in prompt  # the rest of the assembly is unchanged

    from imp.agent.prompt import BASE_PROMPT

    assert build().startswith(BASE_PROMPT.splitlines()[0])  # default unchanged


def test_actual_skill_path_used_when_metadata_name_differs():
    prompt = build_system_prompt(
        "/home", [], {}, [("display", "help", "skills/actual/SKILL.md")], ""
    )
    assert "skills/actual/SKILL.md" in prompt
    assert ".imp/skills/<name>" not in prompt
