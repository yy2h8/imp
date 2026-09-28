"""Telegram Bot API transport: one method per endpoint the assistant uses.

No SDK and no framework — raw POSTs over the shared httpx client family,
long-polling via getUpdates. Message text is markdown: send_message and
edit_message render it as Telegram HTML with a literal-text fallback, so
formatting can never lose a message. Status composition lives in ui.py.
"""

from __future__ import annotations

import asyncio

import httpx2 as httpx

from imp.config import DEFAULT_MAX_HTTP_BYTES

from .markdown import chunks, to_html, units

API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 25  # long-poll server hold; must stay below the HTTP timeout
# transport failures back off exponentially before retrying
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0


class TelegramError(RuntimeError):
    """A Bot API call failed after retries; message describes the cause."""


MAX_MESSAGE_CHARS = 4096

# room for the tags to_html adds (a <pre> pair with a language class), so
# converted chunks fit the cap instead of falling back to plain text
_HTML_HEADROOM = 64


def split(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """Lossless plain-text chunks; count UTF-16 units conservatively."""
    if limit < 2:
        raise ValueError("chunk limit must be at least 2")
    chunks, start, size = [], 0, 0
    for index, char in enumerate(text):
        units = 2 if ord(char) > 0xFFFF else 1
        if size + units > limit:
            chunks.append(text[start:index])
            start, size = index, 0
        size += units
    if start < len(text):
        chunks.append(text[start:])
    return chunks


async def send_text(bot, chat_id: int, text: str) -> list[int]:
    ids = []
    for chunk in chunks(text, MAX_MESSAGE_CHARS - _HTML_HEADROOM):
        message_id = await bot.send_message(chat_id, chunk)
        if message_id is None:
            raise TelegramError("Required message delivery failed")
        ids.append(message_id)
    return ids


class TelegramBot:
    """Bot API client. Required sends raise on rejection or exhausted retries;
    cosmetic edits and typing indicators may fail without aborting a turn."""

    def __init__(
        self,
        token: str,
        timeout: float = 60.0,
        max_attempts: int = 4,
        max_bytes: int = DEFAULT_MAX_HTTP_BYTES,
    ) -> None:
        self.max_bytes = max_bytes
        self.token = token
        self.max_attempts = max_attempts
        self.client = httpx.AsyncClient(timeout=timeout)

    async def call(self, method: str, *, files=None, **payload) -> dict:
        """Shared bounded retry policy for JSON and multipart requests."""
        url = API_BASE.format(token=self.token, method=method)
        last = "unknown failure"
        for attempt in range(self.max_attempts):
            delay = min(BACKOFF_BASE_S * 2**attempt, BACKOFF_MAX_S)
            retry = True
            try:
                kwargs = (
                    {"data": payload, "files": files} if files else {"json": payload}
                )
                response = await self.client.post(url, **kwargs)
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                if response.is_success and data.get("ok"):
                    return data["result"]
                code = data.get("error_code", response.status_code)
                last = f"HTTP/API {code}"
                retry = code == 429 or (isinstance(code, int) and code >= 500)
                if code == 429:
                    parameters = data.get("parameters") or {}
                    requested = parameters.get(
                        "retry_after", response.headers.get("retry-after")
                    )
                    try:
                        delay = (
                            max(delay, float(requested))
                            if requested is not None
                            else delay
                        )
                    except (TypeError, ValueError):
                        pass
            except httpx.HTTPError as exc:
                last = type(exc).__name__  # request URLs contain the bot token
            if not retry or attempt + 1 == self.max_attempts:
                break
            await asyncio.sleep(delay)
        raise TelegramError(f"{method} failed after {attempt + 1} attempts: {last}")

    async def get_updates(self, offset: int) -> list[dict]:
        """Long-poll for updates newer than ``offset`` (empty list on timeout)."""
        updates = await self.call(
            "getUpdates", offset=offset, timeout=POLL_TIMEOUT_S, allowed_updates=[]
        )
        return list(updates)

    async def send_message(self, chat_id: int, text: str) -> int:
        """Markdown in: rendered as Telegram HTML, falling back to the
        literal text when Telegram rejects the rendering or escaping
        outgrows the size cap."""
        if not text or len(text.encode("utf-16-le")) // 2 > MAX_MESSAGE_CHARS:
            raise ValueError("Telegram message must contain 1..4096 UTF-16 units")
        rendered = to_html(text)
        try:
            if units(rendered) <= MAX_MESSAGE_CHARS:
                result = await self._send_raw(chat_id, rendered, "HTML")
            else:  # escaping inflated past the cap: send the text as-is
                result = await self._send_raw(chat_id, text)
        except TelegramError:
            result = await self._send_raw(chat_id, text)
        return result["message_id"]

    async def _send_raw(self, chat_id: int, text: str, parse_mode: str | None = None):
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return await self.call("sendMessage", **payload)

    async def send_text(self, chat_id: int, text: str) -> list[int]:
        return await send_text(self, chat_id, text)

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        plain = split(text)[0] if text else " "
        rendered = to_html(plain)
        payload = {"text": rendered, "parse_mode": "HTML"}
        if units(rendered) > MAX_MESSAGE_CHARS:
            payload = {"text": plain}
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                **payload,
            )
        except TelegramError:
            try:  # a parse rejection still leaves the literal text editable
                await self.call(
                    "editMessageText",
                    chat_id=chat_id,
                    message_id=message_id,
                    text=plain,
                )
            except TelegramError:
                return False
        return True

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action=action)
        except TelegramError:
            pass  # cosmetic; never worth failing a turn over

    async def send_document(
        self, chat_id: int, data: bytes, filename: str, caption: str = ""
    ) -> int:
        result = await self.call(
            "sendDocument",
            files={"document": (filename, data)},
            chat_id=chat_id,
            caption=split(caption, 1024)[0] if caption else "",
        )
        return result["message_id"]

    async def get_me(self) -> dict:
        """The bot's own identity; whoami uses it to validate the token."""
        return await self.call("getMe")

    async def get_file(self, file_id: str) -> dict:
        """Look up a file entry (file_path) for downloading."""
        return await self.call("getFile", file_id=file_id)

    async def download_file(self, file_path: str) -> bytes:
        """Fetch raw bytes for a file previously resolved via get_file."""
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        try:
            async with self.client.stream("GET", url) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > self.max_bytes:
                        raise TelegramError(
                            f"Download exceeds byte limit ({self.max_bytes})"
                        )
                    data.extend(chunk)
                return bytes(data)
        except httpx.HTTPError as exc:
            raise TelegramError(f"Download failed: {type(exc).__name__}") from None

    async def close(self) -> None:
        await self.client.aclose()
