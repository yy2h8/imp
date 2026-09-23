"""Telegram Bot API transport: one method per endpoint the assistant uses.

No SDK and no framework — raw POSTs over the shared httpx client family,
long-polling via getUpdates. Transport only: rendering lives in ui.py.
"""

from __future__ import annotations

import asyncio

import httpx2 as httpx

API_BASE = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT_S = 25  # long-poll server hold; must stay below the HTTP timeout
# transport failures back off exponentially before retrying (spec §4.5)
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 60.0


class TelegramError(RuntimeError):
    """A Bot API call failed after retries; message describes the cause."""


class TelegramBot:
    """Thin async client for the subset of the Bot API the assistant needs:
    getUpdates / sendMessage / editMessageText / sendChatAction / sendDocument /
    getFile. Never raises on Telegram-side rejections mid-turn — senders check
    the returned ``ok`` flag; only exhausted retries raise TelegramError."""

    def __init__(
        self, token: str, timeout: float = 60.0, max_attempts: int = 4
    ) -> None:
        self.token = token
        self.max_attempts = max_attempts
        self.client = httpx.AsyncClient(timeout=timeout)

    async def call(self, method: str, **payload) -> dict:
        """POST a Bot API method; returns its ``result`` object.

        Retryable failures (network, 429/5xx) back off exponentially and are
        retried up to max_attempts; a final failure raises TelegramError.
        """
        url = API_BASE.format(token=self.token, method=method)
        delay = BACKOFF_BASE_S
        last = ""
        for _ in range(self.max_attempts):
            try:
                response = await self.client.post(url, json=payload)
                if response.status_code == 429 or response.status_code >= 500:
                    last = f"HTTP {response.status_code}"
                    retry_after = response.headers.get("retry-after")
                    if retry_after and response.status_code == 429:
                        delay = max(delay, min(float(retry_after), BACKOFF_MAX_S))
                else:
                    data = response.json()
                    if data.get("ok"):
                        return data["result"]
                    last = data.get("description", "unknown Telegram error")
            except (httpx.HTTPError, ValueError) as exc:  # transport / bad JSON
                last = str(exc)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
        raise TelegramError(f"{method} failed after {self.max_attempts} attempts: {last}")

    async def get_updates(self, offset: int) -> list[dict]:
        """Long-poll for updates newer than ``offset`` (empty list on timeout)."""
        updates = await self.call(
            "getUpdates", offset=offset, timeout=POLL_TIMEOUT_S, allowed_updates=[]
        )
        return list(updates)

    async def send_message(self, chat_id: int, text: str) -> int | None:
        """Send a markdown message; returns the message_id (None if rejected)."""
        try:
            result = await self.call(
                "sendMessage",
                chat_id=chat_id,
                text=text[:4096],
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )
        except TelegramError:
            return None
        return result.get("message_id")

    async def edit_message(self, chat_id: int, message_id: int, text: str) -> bool:
        """Edit a sent message in place. Returns False when Telegram refuses
        (e.g. unchanged text); transport errors bubble to the caller."""
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text[:4096],
                parse_mode="Markdown",
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
    ) -> int | None:
        """Upload a file via multipart sendDocument; returns the message_id.
        Returns None when Telegram rejects the upload (e.g. too big)."""
        files = {"document": (filename, data)}
        payload: dict = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption[:1024]
            payload["parse_mode"] = "Markdown"
        delay = BACKOFF_BASE_S
        for _ in range(self.max_attempts):
            # a fresh request each attempt: multipart streams are single-use
            request = self.client.build_request(
                "POST",
                API_BASE.format(token=self.token, method="sendDocument"),
                data=payload,
                files=files,
            )
            try:
                response = await self.client.send(request)
                data_json = response.json()
                if data_json.get("ok"):
                    return data_json["result"].get("message_id")
                return None  # Telegram rejected the upload (e.g. file too big)
            except (httpx.HTTPError, ValueError):
                pass  # transport failure: retry with backoff
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)
        return None  # transport failed after retries: in-band for send_file

    async def get_me(self) -> dict:
        """The bot's own identity; whoami uses it to validate the token."""
        return await self.call("getMe")

    async def get_file(self, file_id: str) -> dict:
        """Look up a file entry (file_path) for downloading."""
        return await self.call("getFile", file_id=file_id)

    async def download_file(self, file_path: str) -> bytes:
        """Fetch raw bytes for a file previously resolved via get_file."""
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        response = await self.client.get(url)
        response.raise_for_status()
        return response.content

    async def close(self) -> None:
        await self.client.aclose()
