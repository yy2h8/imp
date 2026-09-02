from __future__ import annotations

from pathlib import Path

import pytest

from imp.config import (
    DEFAULT_BASE_URL,
    DEFAULT_COMMAND_TIMEOUT,
    DEFAULT_MAX_CONTEXT,
    DEFAULT_MAX_HTTP_BYTES,
    DEFAULT_MAX_ITERATIONS,
    DEFAULT_MAX_REASONING_DISPLAY_LINES,
    DEFAULT_MAX_TOOL_DISPLAY_LINES,
    DEFAULT_MAX_TOOL_OUTPUT,
    DEFAULT_MODEL,
    DEFAULT_NETWORK_TIMEOUT,
    REASONING_EFFORTS,
    Config,
)

ENV_VARS = [
    "OPENAI_API_KEY",
    "OPENAI_MODEL",
    "OPENAI_BASE_URL",
    "BRAVE_API_KEY",
    "IMP_WORKSPACE",
    "IMP_MAX_ITERATIONS",
    "IMP_MAX_CONTEXT",
    "IMP_COMMAND_TIMEOUT",
    "IMP_NETWORK_TIMEOUT",
    "IMP_MAX_TOOL_OUTPUT",
    "IMP_MAX_TOOL_DISPLAY_LINES",
    "IMP_MAX_REASONING_DISPLAY_LINES",
    "IMP_MAX_HTTP_BYTES",
    "IMP_AUTO_APPROVE",
    "IMP_REASONING_EFFORT",
]

INT_VARS = [
    var
    for var in ENV_VARS
    if var.startswith("IMP_")
    and var not in {"IMP_WORKSPACE", "IMP_AUTO_APPROVE", "IMP_REASONING_EFFORT"}
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def test_missing_api_key_raises():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        Config.from_env()


def test_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    config = Config.from_env()
    assert config.api_key == "key"
    assert config.model == DEFAULT_MODEL
    assert config.base_url == DEFAULT_BASE_URL
    assert config.workspace == Path.cwd()
    assert config.max_iterations == DEFAULT_MAX_ITERATIONS
    assert config.max_context == DEFAULT_MAX_CONTEXT
    assert config.command_timeout == DEFAULT_COMMAND_TIMEOUT
    assert config.network_timeout == DEFAULT_NETWORK_TIMEOUT
    assert config.max_tool_output == DEFAULT_MAX_TOOL_OUTPUT
    assert config.max_tool_display_lines == DEFAULT_MAX_TOOL_DISPLAY_LINES
    assert (
        config.max_reasoning_display_lines == DEFAULT_MAX_REASONING_DISPLAY_LINES
    )
    assert config.max_http_bytes == DEFAULT_MAX_HTTP_BYTES
    assert config.auto_approve is False
    assert config.brave_api_key is None
    assert config.reasoning_effort is None


def test_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    values = {
        "OPENAI_MODEL": "m",
        "OPENAI_BASE_URL": "http://localhost:1/v1",
        "BRAVE_API_KEY": "brave",
        "IMP_WORKSPACE": str(tmp_path),
        "IMP_MAX_ITERATIONS": "5",
        "IMP_MAX_CONTEXT": "1000",
        "IMP_COMMAND_TIMEOUT": "10",
        "IMP_NETWORK_TIMEOUT": "20",
        "IMP_MAX_TOOL_OUTPUT": "3000",
        "IMP_MAX_TOOL_DISPLAY_LINES": "4",
        "IMP_MAX_REASONING_DISPLAY_LINES": "3",
        "IMP_MAX_HTTP_BYTES": "1000",
    }
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    for var, value in values.items():
        monkeypatch.setenv(var, value)
    config = Config.from_env()
    assert config.model == "m"
    assert config.base_url == "http://localhost:1/v1"
    assert config.brave_api_key == "brave"
    assert config.workspace == tmp_path
    assert config.max_iterations == 5
    assert config.max_context == 1000
    assert config.command_timeout == 10
    assert config.network_timeout == 20
    assert config.max_tool_output == 3000
    assert config.max_tool_display_lines == 4
    assert config.max_reasoning_display_lines == 3
    assert config.max_http_bytes == 1000


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
@pytest.mark.parametrize("var", INT_VARS)
def test_invalid_int_raises(var: str, value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv(var, value)
    with pytest.raises(ValueError, match=var):
        Config.from_env()


def test_workspace_not_a_directory_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_WORKSPACE", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="not a directory"):
        Config.from_env()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "y", "YES", " True "])
def test_auto_approve_truthy(value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_AUTO_APPROVE", value)
    assert Config.from_env().auto_approve is True


@pytest.mark.parametrize("value", ["", "0", "no", "false"])
def test_auto_approve_falsy(value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_AUTO_APPROVE", value)
    assert Config.from_env().auto_approve is False


def test_explicit_argument_wins_over_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_AUTO_APPROVE", "1")
    assert Config.from_env(auto_approve=False).auto_approve is False


@pytest.mark.parametrize("value", REASONING_EFFORTS)
def test_reasoning_effort_valid(value: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_REASONING_EFFORT", value.upper())
    assert Config.from_env().reasoning_effort == value


@pytest.mark.parametrize("value", ["", "  "])
def test_reasoning_effort_empty_means_unset(
    value: str, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_REASONING_EFFORT", value)
    assert Config.from_env().reasoning_effort is None


def test_reasoning_effort_invalid_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "key")
    monkeypatch.setenv("IMP_REASONING_EFFORT", "turbo")
    with pytest.raises(ValueError, match="IMP_REASONING_EFFORT"):
        Config.from_env()
