"""AssistantConfig: env parsing for the assistant-owned variables."""

from __future__ import annotations

from pathlib import Path

import pytest

from assistant.config import (
    DEFAULT_STT_MODEL,
    DEFAULT_TZ,
    OPENROUTER_BASE_URL,
    AssistantConfig,
)

ASSISTANT_ENV_VARS = [
    "TELEGRAM_BOT_TOKEN",
    "IMP_TG_ALLOWED_USER_IDS",
    "IMP_HOME",
    "IMP_TG_EDIT_INTERVAL",
    "IMP_TG_STATUS_MAX_CHARS",
    "IMP_SESSION_RESET_THRESHOLD",
    "IMP_SCRATCH_TTL_DAYS",
    "IMP_STT_MODEL",
    "IMP_TZ",
]

IMP_ENV_VARS = [
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


def make_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str
) -> AssistantConfig:
    for var in ASSISTANT_ENV_VARS + IMP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("IMP_TG_ALLOWED_USER_IDS", "7")
    monkeypatch.setenv("IMP_HOME", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return AssistantConfig.from_env()


async def test_build_assistant_pins_openrouter_and_wires_stt(tmp_path, monkeypatch):
    """The composition root: base URL pinned to OpenRouter, Config.from_env
    honored (model reaches the client), STT + inbox wired."""
    from assistant.adapters.stt import SttClient
    from assistant.app import build_assistant

    make_config(tmp_path, monkeypatch, OPENAI_MODEL="openai/gpt-5-mini")
    assistant_config = AssistantConfig.from_env()

    async with build_assistant(assistant_config, 7) as app:
        assert app.config.base_url == "https://openrouter.ai/api/v1"
        assert app.config.model == "openai/gpt-5-mini"  # env var respected now
        assert app.config.workspace == tmp_path
        assert isinstance(app.uploads.stt, SttClient)
        assert app.uploads.stt.model == "openai/whisper-large-v3-turbo"
        assert (tmp_path / "inbox").is_dir()
        assert "schedule_job" in app.agent.tools
        assert "unschedule_job" in app.agent.tools
        # the tools saw the same workspace the jobs land in
        assert app.agent.tools["schedule_job"].config.workspace == tmp_path


def test_minimal_config_defaults(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    assert config.bot_token == "t"
    assert config.allowed_user_ids == frozenset({7})
    assert config.home == tmp_path
    assert config.stt_model == DEFAULT_STT_MODEL == "openai/whisper-large-v3-turbo"
    assert config.tz == DEFAULT_TZ == "Asia/Almaty"
    assert OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"


def test_stt_and_tz_overrides(tmp_path, monkeypatch):
    config = make_config(
        tmp_path, monkeypatch, IMP_STT_MODEL="openai/whisper-1", IMP_TZ="UTC"
    )
    assert config.stt_model == "openai/whisper-1"
    assert config.tz == "UTC"


def test_invalid_tz_is_a_config_error(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="IMP_TZ"):
        make_config(tmp_path, monkeypatch, IMP_TZ="Mars/Olympus_Mons")


def test_missing_token_raises(tmp_path, monkeypatch):
    make_config(tmp_path, monkeypatch)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        AssistantConfig.from_env()


def test_raw_token_reads_only_the_token(tmp_path, monkeypatch):
    make_config(tmp_path, monkeypatch)
    assert AssistantConfig.raw_token() == "t"
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    assert AssistantConfig.raw_token() == ""


@pytest.mark.parametrize(
    "env,value",
    [
        ("IMP_TG_ALLOWED_USER_IDS", "0"),
        ("IMP_TG_ALLOWED_USER_IDS", "7,8"),
        ("IMP_TG_STATUS_MAX_CHARS", "0"),
        ("IMP_TG_STATUS_MAX_CHARS", "4097"),
        ("IMP_SCRATCH_TTL_DAYS", "-1"),
    ],
)
def test_invalid_limits(tmp_path, monkeypatch, env, value):
    with pytest.raises(ValueError, match=env):
        make_config(tmp_path, monkeypatch, **{env: value})


async def test_assistant_ignores_cli_workspace(tmp_path, monkeypatch):
    from assistant.app import build_assistant

    config = make_config(tmp_path, monkeypatch, IMP_WORKSPACE=str(tmp_path / "missing"))
    async with build_assistant(config, 7) as app:
        assert app.config.workspace == tmp_path
        assert app.config.model == "openai/gpt-5-mini"


async def test_context_exit_closes_replacement_writer(tmp_path, monkeypatch):
    from assistant.app import build_assistant

    config = make_config(tmp_path, monkeypatch)
    async with build_assistant(config, 7) as app:
        app.reset()
        writer = app.session.writer
    assert writer._fh.closed


async def test_reset_refreshes_manual(tmp_path, monkeypatch):
    from assistant.app import build_assistant

    config = make_config(tmp_path, monkeypatch)
    async with build_assistant(config, 7) as app:
        (tmp_path / "AGENTS.md").write_text("Changed owner instruction")
        app.reset()
        assert "Changed owner instruction" in app.session.context.messages[0].content


async def test_failed_question_delivery_clears_pending(tmp_path, monkeypatch):
    from assistant.adapters.telegram import TelegramError
    from assistant.app import build_assistant

    config = make_config(tmp_path, monkeypatch)
    async with build_assistant(config, 7) as app:

        async def fail(*args):
            raise TelegramError("cannot deliver")

        app.bot.send_message = fail
        with pytest.raises(TelegramError):
            await app.agent.tools["ask"].execute("Question?")
        assert not app.ask_router.pending
