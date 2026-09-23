from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from assistant.adapters.telegram import BACKOFF_BASE_S, TelegramBot, TelegramError


def bot_with(handler, max_attempts: int = 4) -> TelegramBot:
    transport = httpx.MockTransport(handler)
    bot = TelegramBot("TESTTOKEN", max_attempts=max_attempts)
    bot.client = httpx.AsyncClient(transport=transport, timeout=5.0)
    return bot


def api_result(payload: dict):
    return httpx.Response(200, json={"ok": True, "result": payload})


def api_error(description: str, status: int = 400):
    return httpx.Response(status, json={"ok": False, "description": description})


class NoSleep:
    """Cut the exponential backoff out of retry tests."""

    def __init__(self):
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


@pytest.fixture
def no_sleep(monkeypatch):
    sleeper = NoSleep()
    monkeypatch.setattr("assistant.adapters.telegram.asyncio.sleep", sleeper)
    return sleeper


async def test_send_message_returns_message_id(no_sleep):
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path == "/botTESTTOKEN/sendMessage"
        assert payload["parse_mode"] == "Markdown"
        return api_result({"message_id": 7})

    assert await bot_with(handler).send_message(1, "hi") == 7


async def test_send_message_truncates_to_telegram_limit(no_sleep):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert len(json.loads(request.content)["text"]) == 4096
        return api_result({"message_id": 1})

    await bot_with(handler).send_message(1, "x" * 5000)


async def test_send_message_rejection_returns_none_not_raise(no_sleep):
    assert await bot_with(lambda request: api_error("chat not found")) .send_message(
        1, "hi"
    ) is None


async def test_edit_message_maps_failure_to_false(no_sleep):
    bot = bot_with(lambda request: api_error("message is not modified"))
    assert await bot.edit_message(1, 2, "text") is False


async def test_call_retries_5xx_then_succeeds(no_sleep):
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502)
        return api_result({"message_id": 5})

    assert await bot_with(handler).send_message(1, "hi") == 5
    assert calls["n"] == 3
    assert no_sleep.delays[:2] == [BACKOFF_BASE_S, BACKOFF_BASE_S * 2]


async def test_call_honors_429_retry_after(no_sleep):
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "7"})
        return api_result({"message_id": 5})

    assert await bot_with(handler).send_message(1, "hi") == 5
    assert no_sleep.delays[0] == 7.0


async def test_call_raises_telegram_error_after_max_attempts(no_sleep):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(TelegramError, match="failed after 2 attempts"):
        await bot_with(handler, max_attempts=2).get_updates(0)


async def test_send_document_truncates_caption(no_sleep):
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        content_type = request.headers["content-type"]
        boundary = content_type.partition("boundary=")[2]
        for part in (await request.aread()).split(b"--" + boundary.encode()):
            if b'name="caption"' in part:
                captured["caption"] = part.split(b"\r\n\r\n", 1)[1].rsplit(
                    b"\r\n", 1
                )[0]
        return api_result({"message_id": 3})

    bot = bot_with(handler)
    assert await bot.send_document(1, b"data", "f.txt", "c" * 2000) == 3
    assert len(captured["caption"]) == 1024  # Telegram's caption limit


async def test_send_document_rejection_returns_none(no_sleep):
    bot = bot_with(lambda request: api_error("file too big"))
    assert await bot.send_document(1, b"data", "f.txt") is None


async def test_get_updates_parses_results(no_sleep):
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["timeout"] == 25  # the long-poll hold
        return api_result([{"update_id": 41, "message": {}}])

    assert await bot_with(handler).get_updates(40) == [
        {"update_id": 41, "message": {}}
    ]


async def test_send_chat_action_swallows_errors(no_sleep):
    bot = bot_with(lambda request: api_error("nope"))
    await bot.send_chat_action(1, "typing")  # no raise
