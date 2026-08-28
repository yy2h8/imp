from __future__ import annotations

from typing import Any, ClassVar

import pytest

from imp.adapters import FileSystemAdapter, HttpClient
from imp.config import Config
from imp.tools import Tool, ToolResult, build_tools, execute_call
from imp.tools.ask import Ask
from imp.tools.fs import ReadFile, StrReplace, WriteFile
from imp.tools.shell import RunShell


def responder(answer: str):
    async def prompt_user(message: str, markdown: bool = True) -> str:
        return answer

    return prompt_user


def forbidden_prompt(message: str):
    raise AssertionError("prompt_user should not be called")


class Boom(Tool):
    name = "boom"
    description = "raises"
    parameters: ClassVar[dict[str, Any]] = {}

    async def execute(self, **kwargs: Any) -> ToolResult:
        raise RuntimeError("kaboom")


class TestApprovals:
    async def test_declined_write_creates_nothing(self, config, fs):
        tool = WriteFile(config=config, fs=fs, prompt_user=responder("n"))
        result = await tool.execute(path="out.txt", content="data")
        assert result.ok is False
        assert "not approved" in result.content
        assert not (config.workspace / "out.txt").exists()

    @pytest.mark.parametrize("answer", ["y", "yes"])
    async def test_approved_write(self, config, fs, answer):
        tool = WriteFile(config=config, fs=fs, prompt_user=responder(answer))
        result = await tool.execute(path="out.txt", content="data")
        assert result.ok is True
        assert (config.workspace / "out.txt").read_text() == "data"

    async def test_auto_approve_skips_prompt(self, config, fs):
        config.auto_approve = True
        tool = WriteFile(config=config, fs=fs, prompt_user=forbidden_prompt)
        assert (await tool.execute(path="out.txt", content="d")).ok is True

    async def test_str_replace_returns_diff(self, config, fs):
        config.auto_approve = True
        fs.write_text_file("f.txt", "alpha\n")
        tool = StrReplace(config=config, fs=fs, prompt_user=responder("y"))
        result = await tool.execute(path="f.txt", old="alpha", new="beta")
        assert result.ok is True
        assert result.diff is not None
        assert "-alpha" in result.diff
        assert "+beta" in result.diff


class TestReadFile:
    async def test_output_is_line_numbered(self, config, fs):
        fs.write_text_file("f.txt", "alpha\nbeta\n")
        tool = ReadFile(config=config, fs=fs)
        result = await tool.execute(path="f.txt")
        assert result.content == "     1: alpha\n     2: beta\n(lines 1-2 of 2)\n"


class TestAsk:
    async def test_passthrough(self):
        tool = Ask(prompt_user=responder("hello"))
        result = await tool.execute(question="q")
        assert result.ok is True
        assert result.content == "User answered: hello"

    async def test_empty_answer(self):
        tool = Ask(prompt_user=responder(""))
        result = await tool.execute(question="q")
        assert "best judgment" in result.content


def registry_for(config: Config, fs: FileSystemAdapter) -> dict[str, Tool]:
    return build_tools(
        config=config, fs=fs, prompt_user=responder("n"), http=HttpClient(config)
    )


def test_build_tools_default_set(config, fs):
    registry = registry_for(config, fs)
    expected = {
        RunShell.name,
        Ask.name,
        "list_dir",
        "read_file",
        WriteFile.name,
        StrReplace.name,
        "web_fetch",
    }
    assert expected <= set(registry)
    assert "web_search" not in registry


def test_build_tools_with_brave_key(config, fs):
    config.brave_api_key = "brave-key"
    assert "web_search" in registry_for(config, fs)


async def test_execute_call_unknown_tool():
    result = await execute_call({}, "nope", {})
    assert result.ok is False
    assert "not found" in result.content.lower()


async def test_execute_call_catches_exceptions():
    result = await execute_call({"boom": Boom()}, "boom", {})
    assert result.ok is False
    assert "boom failed: kaboom" == result.content


async def test_run_shell_smoke(config):
    config.auto_approve = True
    tool = RunShell(config=config, prompt_user=responder("n"))
    result = await tool.execute(command="echo imp")
    assert result.ok is True
    assert '"exit_code": 0' in result.content
    assert "imp" in result.content
