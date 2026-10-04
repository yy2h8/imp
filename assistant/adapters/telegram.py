"""Telegram transport over python-telegram-bot: one method per endpoint the assistant
uses. No parse_mode anywhere — markdown is rendered to (text, entities) by
adapters.markdown, with literal-text fallbacks when Telegram rejects an
entity rendering, so formatting can never lose a message. Status composition
lives in ui.py; long-answer splitting in markdown.render_long.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable
from datetime import timedelta
from typing import BinaryIO

import httpx2
from telegram import Bot, InputFile, MessageEntity
from telegram.error import RetryAfter
from telegram.error import TelegramError as BotAPIError
from telegram.request import HTTPXRequest

from imp.adapters.http import HttpClient
from imp.config import DEFAULT_MAX_HTTP_BYTES

from .markdown import render, render_long

POLL_TIMEOUT_S = 25  # long-poll server hold

MAX_MESSAGE_CHARS = 4096
CAPTION_CHARS = 1024


class TelegramError(RuntimeError):
    """A Bot API call failed; message describes the cause."""

    def __init__(self, message: str, retry_after: float = 5.0) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def units(text: str) -> int:
    """UTF-16 code units: Telegram's size currency."""
    return len(text.encode("utf-16-le")) // 2


def split(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Lossless plain-text chunks; count UTF-16 units conservatively."""
    if limit < 2:
        raise ValueError("chunk limit must be at least 2")
    chunks, start, size = [], 0, 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if size + char_units > limit:
            chunks.append(text[start:index])
            start, size = index, 0
        size += char_units
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def _entities(entities: list[dict]) -> list[MessageEntity]:
    return [MessageEntity(**entity) for entity in entities]


def _as_dict(result):
    """SDK objects → portable dictionaries used by the durable intake."""
    if isinstance(result, dict):
        return result
    if isinstance(result, (list, tuple)):
        return [_as_dict(item) for item in result]
    return result.to_dict()


class TelegramBot:
    """Bot wrapper. Required sends raise TelegramError on rejection;
    cosmetic edits and typing indicators may fail without aborting a turn."""

    def __init__(
        self,
        token: str,
        timeout: float = 60.0,
        max_bytes: int = DEFAULT_MAX_HTTP_BYTES,
        client=None,
        http: HttpClient | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.token = token
        self.http = http
        self.client = client if client is not None else Bot(
            token=token,
            request=HTTPXRequest(read_timeout=timeout, media_write_timeout=timeout),
            get_updates_request=HTTPXRequest(read_timeout=timeout),
        )

    async def initialize(self) -> None:
        await self._call("initialize", self.client.initialize())

    async def _call(self, method: str, coro: Awaitable):
        try:
            return await coro
        except BotAPIError as exc:
            delay = exc.retry_after if isinstance(exc, RetryAfter) else 5.0
            if isinstance(delay, timedelta):
                delay = delay.total_seconds()
            message = str(exc).replace(self.token, "<redacted>")
            raise TelegramError(f"{method} failed: {message}", delay) from None

    async def send_message(self, chat_id: int, text: str) -> int:
        """Markdown in: rendered as text + entities, falling back to the
        literal text when Telegram rejects the rendering or the input is
        oversize."""
        if not text or units(text) > MAX_MESSAGE_CHARS:
            raise ValueError("Telegram message must contain 1..4096 UTF-16 units")
        rendered, entities = render(text)
        try:
            return (
                await self._call(
                    "sendMessage",
                    self.client.send_message(
                        chat_id=chat_id,
                        text=rendered,
                        entities=_entities(entities),
                        disable_web_page_preview=True,
                    ),
                )
            ).message_id
        except TelegramError:
            result = await self._call(
                "sendMessage",
                self.client.send_message(
                    chat_id=chat_id, text=text, disable_web_page_preview=True
                ),
            )
            return result.message_id

    async def send_text(self, chat_id: int, text: str) -> list[int]:
        """Long/markdown answers: render_long items — texts as entity
        messages, extracted code files as documents."""
        ids: list[int] = []
        for item in await render_long(text):
            if hasattr(item, "file_data"):
                ids.append(
                    await self.send_document(
                        chat_id,
                        item.file_data,
                        item.file_name,
                        caption=item.caption,
                    )
                )
            else:
                if not item.text.strip():
                    continue
                result = await self._call(
                    "sendMessage",
                    self.client.send_message(
                        chat_id=chat_id,
                        text=item.text,
                        entities=_entities(item.entities),
                        disable_web_page_preview=True,
                    ),
                )
                ids.append(result.message_id)
        return ids or [await self.send_message(chat_id, text)]

    async def edit_message(
        self, chat_id: int, message_id: int, text: str
    ) -> bool:
        """Edit the status message with entities; a parse rejection retries
        the literal text; both failing returns False (cosmetic)."""
        plain = split(text)[0] if text else " "
        rendered, entities = render(plain)
        try:
            await self._call(
                "editMessageText",
                self.client.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=rendered,
                    entities=_entities(entities),
                ),
            )
            return True
        except TelegramError:
            try:
                await self._call(
                    "editMessageText",
                    self.client.edit_message_text(
                        chat_id=chat_id, message_id=message_id, text=plain
                    ),
                )
                return True
            except TelegramError:
                return False

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self._call(
                "sendChatAction",
                self.client.send_chat_action(chat_id=chat_id, action=action),
            )
        except TelegramError:
            pass  # cosmetic; never worth failing a turn over

    async def send_document(
        self, chat_id: int, data: bytes | BinaryIO, filename: str, caption: str = ""
    ) -> int:
        result = await self._call(
            "sendDocument",
            self.client.send_document(
                chat_id=chat_id,
                document=InputFile(data, filename=filename, read_file_handle=False),
                caption=split(caption, CAPTION_CHARS)[0] if caption else "",
            ),
        )
        return result.message_id

    async def get_me(self) -> dict:
        """The bot's own identity; whoami uses it to validate the token."""
        return _as_dict(await self._call("getMe", self.client.get_me()))

    async def get_updates(self, offset: int) -> list[dict]:
        """Long-poll for updates newer than ``offset`` (empty list on timeout)."""
        updates = await self._call(
            "getUpdates",
            self.client.get_updates(
                offset=offset, timeout=POLL_TIMEOUT_S, allowed_updates=["message"]
            ),
        )
        return _as_dict(list(updates))

    async def get_file(self, file_id: str) -> dict:
        """Look up a file entry (file_path) for downloading."""
        return _as_dict(
            await self._call("getFile", self.client.get_file(file_id=file_id))
        )

    async def download(self, file_id: str) -> AsyncIterator[bytes]:
        """Stream to the sandbox writer; reject oversized files before buffering."""
        if self.http is None:
            raise TelegramError("Download requires the shared HTTP client")
        file = await self.get_file(file_id)
        if (file.get("file_size") or 0) > self.max_bytes:
            raise TelegramError(f"Download exceeds byte limit ({self.max_bytes})")
        url = file.get("file_path")
        if not url:
            raise TelegramError("Telegram did not return a download URL")
        try:
            # PTB's download_to_drive also buffers the response; use our existing
            # streaming client, including its SSRF validation, for the file body.
            async with self.http.client.stream("GET", url) as response:
                response.raise_for_status()
                received = 0
                async for chunk in response.aiter_bytes():
                    received += len(chunk)
                    if received > self.max_bytes:
                        raise TelegramError(f"Download exceeds byte limit ({self.max_bytes})")
                    yield chunk
        except (httpx2.HTTPError, ValueError) as exc:
            message = str(exc).replace(self.token, "<redacted>")
            raise TelegramError(f"Download failed: {message}") from None

    async def close(self) -> None:
        await self.client.shutdown()


async def send_text(bot: TelegramBot, chat_id: int, text: str) -> list[int]:
    """Required delivery: raise on failure, never silently drop."""
    ids = await bot.send_text(chat_id, text)
    if not ids or any(i is None for i in ids):
        raise TelegramError("Required message delivery failed")
    return ids
