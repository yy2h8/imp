"""Telegram transport over aiogram: one method per endpoint the assistant
uses. No parse_mode anywhere — markdown is rendered to (text, entities) by
adapters.markdown, with literal-text fallbacks when Telegram rejects an
entity rendering, so formatting can never lose a message. Status composition
lives in ui.py; long-answer splitting in markdown.render_long.
"""

from __future__ import annotations

from collections.abc import Awaitable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile, MessageEntity

from imp.config import DEFAULT_MAX_HTTP_BYTES

from .markdown import render, render_long

POLL_TIMEOUT_S = 25  # long-poll server hold

MAX_MESSAGE_CHARS = 4096
CAPTION_CHARS = 1024


class TelegramError(RuntimeError):
    """A Bot API call failed; message describes the cause."""


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
    """aiogram typed results → plain dicts (fakes may already return dicts)."""
    if isinstance(result, dict):
        return result
    if isinstance(result, list):
        return [_as_dict(item) for item in result]
    dumper = getattr(result, "model_dump", None)
    return dumper(exclude_none=True) if dumper else result


class TelegramBot:
    """aiogram Bot wrapper. Required sends raise TelegramError on rejection;
    cosmetic edits and typing indicators may fail without aborting a turn."""

    def __init__(
        self,
        token: str,
        timeout: float = 60.0,
        max_bytes: int = DEFAULT_MAX_HTTP_BYTES,
        client=None,
    ) -> None:
        self.max_bytes = max_bytes
        self.token = token
        self.client = client if client is not None else Bot(
            token=token, request_timeout=timeout
        )

    async def _call(self, method: str, coro: Awaitable):
        try:
            return await coro
        except TelegramAPIError as exc:
            raise TelegramError(f"{method} failed: {exc}") from None

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
        self, chat_id: int, data: bytes, filename: str, caption: str = ""
    ) -> int:
        result = await self._call(
            "sendDocument",
            self.client.send_document(
                chat_id=chat_id,
                document=BufferedInputFile(data, filename=filename),
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
                offset=offset, timeout=POLL_TIMEOUT_S, allowed_updates=[]
            ),
        )
        return _as_dict(list(updates))

    async def get_file(self, file_id: str) -> dict:
        """Look up a file entry (file_path) for downloading."""
        return _as_dict(
            await self._call("getFile", self.client.get_file(file_id=file_id))
        )

    async def download(self, file_id: str) -> bytes:
        """Resolve and fetch raw bytes for one attachment, enforcing the
        configured byte cap (bounded by Telegram's 20 MB platform limit)."""
        data = await self._call(
            "download",
            self.client.download(file=file_id),
        )
        payload = data.getvalue() if hasattr(data, "getvalue") else data.read()
        if len(payload) > self.max_bytes:
            raise TelegramError(f"Download exceeds byte limit ({self.max_bytes})")
        return payload

    async def download_file(self, file_path: str) -> bytes:
        """Fetch raw bytes for a resolved file_path (v1 seam; uploads)."""
        data = await self._call(
            "downloadFile",
            self.client.download_file(file_path),
        )
        payload = data.getvalue() if hasattr(data, "getvalue") else data.read()
        if len(payload) > self.max_bytes:
            raise TelegramError(f"Download exceeds byte limit ({self.max_bytes})")
        return payload

    async def close(self) -> None:
        session = getattr(self.client, "session", None)
        if session is not None:
            await session.close()


async def send_text(bot: TelegramBot, chat_id: int, text: str) -> list[int]:
    """Required delivery: raise on failure, never silently drop."""
    ids = await bot.send_text(chat_id, text)
    if not ids or any(i is None for i in ids):
        raise TelegramError("Required message delivery failed")
    return ids
