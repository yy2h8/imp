from __future__ import annotations

import platform
from datetime import UTC, datetime

from ..tools import Tool

BASE_PROMPT = """# imp

You are a pragmatic coding assistant operating in a ReAct loop:
reason about the task, call tools to act, observe results, repeat.

How you work:
- Work only on the user's requested task.
- Think in small, verifiable steps.
- Use tools for facts. Do not guess file contents, command output, or web facts when you can inspect or search.
- Inspect before changing: read relevant files and run read-only commands first.
- Prefer the smallest correct change. Make focused, minimal edits.
- Verify meaningful changes: compile, run tests, linters, or the program itself.
- Use web_search/web_fetch when you need current or external information.
- Use skills when appropriate or requested by the user.
- If a tool call fails, read the error, adapt, and try a different approach.
- When the task is done, reply with markdown text and no tool calls.

Reporting:
- Do not claim success without reasonable verification.
- Be concise in final answers. Summarize what you did and why, and mention any caveats.
  Use tool results to ground your claims.
- Never expose API keys or secrets. Do not echo credentials from the environment.
- Use markdown formatting for text messages."""

SKILL_INSTRUCTIONS = """Skills are directories under `.imp/skills/<name>/SKILL.md` containing detailed instructions.
The list above shows only name and description — that's all you have until you load one.

- If a skill's description matches the current task, call read_file with
  path=".imp/skills/<name>/SKILL.md" before proceeding, and follow its instructions.
- SKILL.md may reference other files in the same directory (e.g. REFERENCE.md, scripts/).
  Only read or run those if the task actually needs them.
- If a script is mentioned, run it with run_shell rather than reproducing its logic yourself.
- Don't load a skill "just in case" — only when its description matches what you're doing.
"""


def _environment_block(workspace: str, fs_listing: list[str]) -> str:
    listing = "".join(f"  - {entry}\n" for entry in fs_listing).rstrip()
    return (
        "You are running with the following environment:\n"
        f"- OS: {platform.system()} {platform.release()} ({platform.version()})\n"
        f"- Python: {platform.python_version()}\n"
        f"- Workspace: {workspace}\n"
        f"- Current UTC datetime: {datetime.now(UTC).isoformat()}\n"
        "- Top-level workspace listing:\n"
        f"{listing}"
    )


def _format_skills(skills: list[tuple]) -> str:
    lines = []
    for s in skills:
        if len(s) == 1:
            lines.append(f"- {s[0]}")
        elif len(s) == 2:
            lines.append(f"- **{s[0]}** - {s[1]}")
    return "\n".join(lines)


def _format_tool_descriptions(tools: dict[str, Tool]) -> str:
    return "\n".join(f"- **{t.name}** - {t.description}" for t in tools.values())


def _format_tool_instructions(tools: dict[str, Tool]) -> str:
    instructions = "\n".join(
        f"- {t.instructions}" for t in tools.values() if t.instructions
    )
    return instructions if instructions.strip() else ""


def build_system_prompt(
    workspace: str,
    fs_listing: list[str],
    tools: dict[str, Tool],
    skills: list[tuple],
    context: str,
) -> str:
    sections: list[str] = [BASE_PROMPT]
    sections.append(f"## Environment\n{_environment_block(workspace, fs_listing)}")

    if tools:
        sections.append(f"## Available Tools\n{_format_tool_descriptions(tools)}")
        tool_instructions = _format_tool_instructions(tools)
        if tool_instructions:
            sections.append(f"## Tool Instructions\n{tool_instructions}")

    if skills:
        sections.append(f"## Available Skills\n{_format_skills(skills)}")
        sections.append(f"## Using Skills\n{SKILL_INSTRUCTIONS.rstrip()}")

    if context.strip():
        sections.append(f"## Project Instructions\n{context}")

    return "\n\n".join(sections)
