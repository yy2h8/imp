"""AssistantConfig: assistant-owned env vars; imp limits reused unchanged."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_HOME = "~/assistant"
DEFAULT_EDIT_INTERVAL = 2.5
DEFAULT_STATUS_MAX_CHARS = 3500
DEFAULT_RESET_THRESHOLD = 0.85
DEFAULT_SCRATCH_TTL_DAYS = 7
DEFAULT_TZ = "Asia/Almaty"  # the owner's timezone; IMP_TZ overrides
DEFAULT_STT_MODEL = "openai/whisper-large-v3-turbo"  # OpenRouter's whisper turbo
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"  # the only provider


@dataclass(slots=True)
class AssistantConfig:
    """Assistant-specific settings. The imp `Config` is built separately
    (a plain dataclass) sharing this process's environment."""

    bot_token: str
    allowed_user_ids: frozenset[int]
    home: Path
    edit_interval: float = DEFAULT_EDIT_INTERVAL
    status_max_chars: int = DEFAULT_STATUS_MAX_CHARS
    reset_threshold: float = DEFAULT_RESET_THRESHOLD
    scratch_ttl_days: int = DEFAULT_SCRATCH_TTL_DAYS
    stt_model: str = DEFAULT_STT_MODEL
    tz: str = DEFAULT_TZ

    @classmethod
    def raw_token(cls) -> str:
        """The bot token from the environment; whoami needs only this."""
        return os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

    @classmethod
    def from_env(cls) -> AssistantConfig:
        token = cls.raw_token()
        if not token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not set. Create a bot with @BotFather and retry."
            )
        raw_ids = os.getenv("IMP_TG_ALLOWED_USER_IDS", "").strip()
        if not raw_ids:
            raise ValueError(
                "IMP_TG_ALLOWED_USER_IDS is not set. "
                "Add the owner's Telegram user id (comma-separated)."
            )
        try:
            allowed = frozenset(
                int(part) for part in raw_ids.split(",") if part.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "IMP_TG_ALLOWED_USER_IDS must be comma-separated integers"
            ) from exc
        if len(allowed) != 1 or next(iter(allowed)) <= 0:
            raise ValueError(
                "IMP_TG_ALLOWED_USER_IDS must contain exactly one positive owner id"
            )

        home = Path(os.getenv("IMP_HOME") or DEFAULT_HOME).expanduser().resolve()
        tz = os.getenv("IMP_TZ") or DEFAULT_TZ
        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"IMP_TZ is not a valid IANA timezone: {tz}") from exc

        def _float(name: str, default: float) -> float:
            try:
                value = float(os.getenv(name) or default)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number") from exc
            if not 0 < value <= 60:
                raise ValueError(f"{name} must be in (0, 60] seconds")
            return value

        def _fraction(name: str, default: float) -> float:
            try:
                value = float(os.getenv(name) or default)
            except ValueError as exc:
                raise ValueError(f"{name} must be a number") from exc
            if not 0 < value < 1:
                raise ValueError(f"{name} must be a fraction in (0, 1)")
            return value

        status_size = int(
            os.getenv("IMP_TG_STATUS_MAX_CHARS") or DEFAULT_STATUS_MAX_CHARS
        )
        ttl = int(os.getenv("IMP_SCRATCH_TTL_DAYS") or DEFAULT_SCRATCH_TTL_DAYS)
        if not 1 <= status_size <= 4096:
            raise ValueError("IMP_TG_STATUS_MAX_CHARS must be in 1..4096")
        if ttl <= 0:
            raise ValueError("IMP_SCRATCH_TTL_DAYS must be positive")
        return cls(
            bot_token=token,
            allowed_user_ids=allowed,
            home=home,
            edit_interval=_float("IMP_TG_EDIT_INTERVAL", DEFAULT_EDIT_INTERVAL),
            status_max_chars=status_size,
            reset_threshold=_fraction(
                "IMP_SESSION_RESET_THRESHOLD", DEFAULT_RESET_THRESHOLD
            ),
            scratch_ttl_days=ttl,
            stt_model=os.getenv("IMP_STT_MODEL") or DEFAULT_STT_MODEL,
            tz=tz,
        )
