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
    "IMP_TRANSCRIPT_ITEM_TTL_DAYS",
    "IMP_TRANSCRIPT_TTL_DAYS",
    "IMP_STT_MODEL",
    "IMP_TZ",
    "IMP_LOG_LEVEL",
    "IMP_MAX_CONCURRENT_JOBS",
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
    async def get_me(_bot):
        from telegram import User
        return User(id=1, is_bot=True, first_name="Test")

    monkeypatch.setattr("telegram.Bot.get_me", get_me)
    for var in ASSISTANT_ENV_VARS + IMP_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST-TOKEN")
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

    make_config(tmp_path, monkeypatch, OPENAI_MODEL="openai/gpt-5-mini", IMP_TZ="Asia/Tokyo")
    assistant_config = AssistantConfig.from_env()

    async with build_assistant(assistant_config, 7) as app:
        assert app.config.base_url == "https://openrouter.ai/api/v1"
        assert app.config.model == "openai/gpt-5-mini"  # env var respected now
        assert app.config.workspace == tmp_path
        assert "Asia/Tokyo" in app.session.context.messages[0].content
        assert isinstance(app.uploads.stt, SttClient)
        assert app.uploads.stt.model == "openai/whisper-large-v3-turbo"
        assert (tmp_path / "inbox").is_dir()
        assert app.db is not None and app.outbox is not None and app.scheduler is not None
        assert app.http is not None
        assert not hasattr(app, "execution_lock")
        assert "schedule_job" in app.agent.tools
        assert "unschedule_job" in app.agent.tools
        assert "memory_set" in app.agent.tools
        assert "list_jobs" in app.agent.tools
        assert "## Memory" not in app.build_prompt()
        assert (await app.agent.tools["memory_set"].execute(
            key="units", value="metric"
        )).ok
        assert "## Memory" in app.build_prompt()
        assert "units: metric" in app.build_prompt()
        # the tools saw the same workspace the jobs land in
        assert app.agent.tools["schedule_job"].config.workspace == tmp_path


def test_max_concurrent_jobs_default(tmp_path, monkeypatch):
    from assistant.config import DEFAULT_MAX_CONCURRENT_JOBS

    config = make_config(tmp_path, monkeypatch)
    assert config.max_concurrent_jobs == DEFAULT_MAX_CONCURRENT_JOBS


def test_max_concurrent_jobs_override(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch, IMP_MAX_CONCURRENT_JOBS="5")
    assert config.max_concurrent_jobs == 5


def test_max_concurrent_jobs_must_be_positive(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="IMP_MAX_CONCURRENT_JOBS"):
        make_config(tmp_path, monkeypatch, IMP_MAX_CONCURRENT_JOBS="0")


def test_transcript_ttl_defaults(tmp_path, monkeypatch):
    from assistant.config import (
        DEFAULT_TRANSCRIPT_ITEM_TTL_DAYS,
        DEFAULT_TRANSCRIPT_TTL_DAYS,
    )

    config = make_config(tmp_path, monkeypatch)
    assert config.transcript_item_ttl_days == DEFAULT_TRANSCRIPT_ITEM_TTL_DAYS == 7
    assert config.transcript_ttl_days == DEFAULT_TRANSCRIPT_TTL_DAYS == 90


def test_transcript_ttl_overrides(tmp_path, monkeypatch):
    config = make_config(
        tmp_path,
        monkeypatch,
        IMP_TRANSCRIPT_ITEM_TTL_DAYS="3",
        IMP_TRANSCRIPT_TTL_DAYS="30",
    )
    assert config.transcript_item_ttl_days == 3
    assert config.transcript_ttl_days == 30



def test_minimal_config_defaults(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    assert config.bot_token == "123456:TEST-TOKEN"
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


def test_log_level_defaults_to_info(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch)
    assert config.log_level == "info"


def test_log_level_override_is_normalized(tmp_path, monkeypatch):
    config = make_config(tmp_path, monkeypatch, IMP_LOG_LEVEL="DEBUG")
    assert config.log_level == "debug"


def test_invalid_log_level_is_a_config_error(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="IMP_LOG_LEVEL"):
        make_config(tmp_path, monkeypatch, IMP_LOG_LEVEL="chatty")


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
    assert AssistantConfig.raw_token() == "123456:TEST-TOKEN"
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
        ("IMP_TRANSCRIPT_ITEM_TTL_DAYS", "0"),
        ("IMP_TRANSCRIPT_TTL_DAYS", "-5"),
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
    assert writer._conn is None


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
