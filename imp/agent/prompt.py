from __future__ import annotations

import platform
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..tools import Tool

BASE_PROMPT = """# imp

You are a pragmatic coding assistant operating in a ReAct loop:
reason about the task, call tools to act, observe results, repeat.

How you work:
- Complete the user's requested task. You may briefly flag obvious adjacent issues in the final reply,
  but investigate or fix them only when needed for the task or requested by the user.
- Think in small, verifiable steps.
- Use tools for facts. Do not guess file contents, command output, or web facts when you can inspect or search.
- Inspect before changing: read relevant files and run read-only commands first.
- Prefer the smallest correct change. Make focused, minimal edits.
- Verify meaningful changes: compile, run tests, linters, or the program itself.
- Use web_search/web_fetch when you need current or external information.
- Use skills when appropriate or requested by the user.
- If a tool call fails, read the error and adapt. After 3 failed attempts at the same action
  in a turn, including attempts with different tools, stop and report what you tried,
  the blocker, and any unfinished work.
- When the task is done, reply with markdown text and no tool calls.

Reporting:
- Do not claim success without reasonable verification.
- Be concise in final answers. Summarize what you did and why, and mention any caveats.
  Use tool results to ground your claims.
- Never expose API keys or secrets. Do not echo credentials from the environment.
- Use markdown formatting for text messages."""

SKILL_INSTRUCTIONS = """Skills contain detailed instructions at the paths listed above.
The list above shows only name and description — that's all you have until you load one.

- If a skill's description matches the current task, call read_file with
  the listed path before proceeding, and follow its instructions.
- SKILL.md may reference other files in the same directory (e.g. REFERENCE.md, scripts/).
  Only read or run those if the task actually needs them.
- If a script is mentioned, run it with run_shell rather than reproducing its logic yourself.
- Don't load a skill "just in case" — only when its description matches what you're doing.
"""


def _environment_block(workspace: str, fs_listing: list[str], timezone: str) -> str:
    listing = "".join(f"  - {entry}\n" for entry in fs_listing).rstrip()
    now = datetime.now(UTC if timezone == "UTC" else ZoneInfo(timezone))
    return (
        "You are running with the following environment:\n"
        f"- OS: {platform.system()} {platform.release()}\n"
        f"- Python: {platform.python_version()}\n"
        f"- Workspace: {workspace}\n"
        f"- Prompt generated at: {now.isoformat(timespec='seconds')} ({timezone})\n"
        "- Top-level workspace listing:\n"
        f"{listing}"
    )


def _format_skills(skills: list[tuple]) -> str:
    lines = []
    for s in skills:
        if len(s) == 1:
            lines.append(f"- {s[0]} (path: `.imp/skills/{s[0]}/SKILL.md`)")
        elif len(s) >= 2:
            path = s[2] if len(s) > 2 else f".imp/skills/{s[0]}/SKILL.md"
            lines.append(f"- **{s[0]}** - {s[1]} (path: `{path}`)")
    return "\n".join(lines)


def _format_tool_instructions(tools: dict[str, Tool]) -> str:
    instructions = "\n".join(
        f"- **{t.name}**: {t.instructions}" for t in tools.values() if t.instructions
    )
    return instructions if instructions.strip() else ""


def build_system_prompt(
    workspace: str,
    fs_listing: list[str],
    tools: dict[str, Tool],
    skills: list[tuple],
    context: str,
    base_prompt: str = BASE_PROMPT,
    *,
    timezone: str = "UTC",
) -> str:
    sections: list[str] = [base_prompt]
    sections.append(f"## Environment\n{_environment_block(workspace, fs_listing, timezone)}")

    if tools:
        tool_instructions = _format_tool_instructions(tools)
        if tool_instructions:
            sections.append(f"## Tool Instructions\n{tool_instructions}")

    if skills:
        sections.append(f"## Available Skills\n{_format_skills(skills)}")
        sections.append(f"## Using Skills\n{SKILL_INSTRUCTIONS.rstrip()}")

    if context.strip():
        sections.append(f"## Project Instructions\n{context}")

    return "\n\n".join(sections)
