"""Assistant adapters: Telegram transport, STT, and event rendering."""

from __future__ import annotations

from .stt import SttClient
from .telegram import TelegramBot, TelegramError
from .ui import StatusBuffer, TelegramUIAdapter

__all__ = ["StatusBuffer", "SttClient", "TelegramBot", "TelegramError", "TelegramUIAdapter"]
