"""Telegram transport: entity rendering, SDK conversion and bounded transfers."""

from __future__ import annotations

import io
from types import SimpleNamespace

import httpx2
import pytest
from telegram import Update
from telegram.error import BadRequest

from assistant.adapters.telegram import TelegramBot, TelegramError


class FakeClient:
    """Records Bot API calls; scriptable per-method results/errors."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.rules: dict[str, list] = {}

    def script(self, method: str, *outcomes) -> None:
        self.rules[method] = list(outcomes)

    async def _record(self, method: str, **kwargs):
        self.calls.append((method, kwargs))
        rule = self.rules.get(method)
        if rule:
            outcome = rule.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        raise AssertionError(f"unexpected call: {method}")

    async def send_message(self, chat_id, text, **kwargs):
        return await self._record(
            "send_message", chat_id=chat_id, text=text, **kwargs
        )

    async def edit_message_text(self, **kwargs):
        return await self._record("edit_message_text", **kwargs)

    async def send_chat_action(self, **kwargs):
        return await self._record("send_chat_action", **kwargs)

    async def send_document(self, **kwargs):
        return await self._record("send_document", **kwargs)

    async def get_me(self, **kwargs):
        return await self._record("get_me", **kwargs)

    async def get_updates(self, **kwargs):
        return await self._record("get_updates", **kwargs)

    async def get_file(self, **kwargs):
        return await self._record("get_file", **kwargs)


class Msg:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


def bad_request(message: str = "can't parse entities") -> BadRequest:
    return BadRequest(message)


def bot(client: FakeClient, max_bytes: int = 10_000_000) -> TelegramBot:
    return TelegramBot("TESTTOKEN", max_bytes=max_bytes, client=client)


def kinds(client: FakeClient, method: str) -> list[dict]:
    return [kwargs for name, kwargs in client.calls if name == method]


async def test_send_message_uses_entities_without_parse_mode():
    client = FakeClient()
    client.script("send_message", Msg(7))
    assert await bot(client).send_message(1, "**bold**") == 7
    (kwargs,) = kinds(client, "send_message")
    assert [e.type for e in kwargs["entities"]] == ["bold"]
    assert not kwargs.get("parse_mode")


async def test_send_message_falls_back_to_plain_on_entity_rejection():
    client = FakeClient()
    client.script("send_message", bad_request(), Msg(9))
    assert await bot(client).send_message(1, "**kept**") == 9
    first, second = kinds(client, "send_message")
    assert first["entities"]
    assert second.get("entities") is None
    assert second["text"] == "**kept**"  # literal markdown text delivered


async def test_send_message_rejects_oversize_input():
    client = FakeClient()
    with pytest.raises(ValueError):
        await bot(client).send_message(1, "x" * 5000)
    assert kinds(client, "send_message") == []


async def test_edit_message_renders_entities_then_plain_false():
    client = FakeClient()
    client.script("edit_message_text", bad_request(), bad_request())
    assert await bot(client).edit_message(1, 2, "*error:* boom") is False
    first, second = kinds(client, "edit_message_text")
    assert [e.type for e in first["entities"]] == ["italic"]
    assert second.get("entities") is None


async def test_edit_message_succeeds_with_entities():
    client = FakeClient()
    client.script("edit_message_text", Msg(2))
    assert await bot(client).edit_message(1, 2, "🧠 working") is True


async def test_send_chat_action_swallows_errors():
    client = FakeClient()
    client.script("send_chat_action", bad_request("nope"))
    await bot(client).send_chat_action(1, "typing")  # never raises


async def test_get_me_and_get_updates_return_dicts():
    client = FakeClient()
    client.script("get_me", {"id": 42, "username": "my_bot"})
    client.script("get_updates", (Update(update_id=41),))
    assert await bot(client).get_me() == {"id": 42, "username": "my_bot"}
    assert await bot(client).get_updates(40) == [{"update_id": 41}]


async def test_send_document_truncates_caption():
    client = FakeClient()
    client.script("send_document", Msg(3))
    assert await bot(client).send_document(1, b"data", "f.txt", "c" * 2000) == 3
    (kwargs,) = kinds(client, "send_document")
    assert len(kwargs["caption"].encode("utf-16-le")) // 2 <= 1024
    assert kwargs["document"].filename == "f.txt"


async def test_download_streams_and_stops_at_limit():
    client = FakeClient()
    client.script("get_file", {"file_path": "https://api.telegram.org/file/botTESTTOKEN/f"})
    chunks_read = []

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self):
            for chunk in (b"first", b"overflow", b"never"):
                chunks_read.append(chunk)
                yield chunk

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda request: httpx2.Response(200, stream=Stream())
    )) as http:
        transport = bot(client, max_bytes=8)
        transport.http = SimpleNamespace(client=http)
        received = []
        with pytest.raises(TelegramError, match="limit"):
            async for chunk in transport.download("f1"):
                received.append(chunk)
    assert received == [b"first"]
    assert chunks_read == [b"first", b"overflow"]
    assert kinds(client, "get_file")[0]["file_id"] == "f1"


async def test_send_document_does_not_read_file_into_memory():
    class Stream(io.BytesIO):
        def read(self, size=-1):
            assert size >= 0, "file must not be eagerly read"
            return super().read(size)

    client = FakeClient()
    client.script("send_document", Msg(3))
    with Stream(b"contents") as stream:
        assert await bot(client).send_document(1, stream, "f.txt") == 3
        document = kinds(client, "send_document")[0]["document"]
        assert document.input_file_content is stream
        assert not stream.closed


async def test_send_text_routes_text_and_files():
    client = FakeClient()
    client.script("send_message", Msg(1))
    client.script("send_document", Msg(2))
    long_md = (
        "explanation\n\n```python\n"
        + "\n".join(f"print({i})" for i in range(300))
        + "\n```"
    )
    ids = await bot(client).send_text(1, long_md)
    assert ids == [1, 2]
    assert kinds(client, "send_message") and kinds(client, "send_document")


async def test_whoami_style_polling_works():
    """get_updates passes the long-poll timeout and offset through."""
    client = FakeClient()
    client.script("get_updates", [])
    assert await bot(client).get_updates(7) == []
    (kwargs,) = kinds(client, "get_updates")
    assert kwargs["offset"] == 7
    assert kwargs["timeout"] >= 25


async def test_real_sdk_serialization_streaming_and_lifecycle():
    import json
    from urllib.parse import parse_qs

    import httpx
    from telegram import Bot
    from telegram.request import HTTPXRequest

    reads, requests = [], []

    class Stream(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            reads.append(size)
            return super().read(size)

    message = {
        "message_id": 7, "date": 1,
        "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "chat": {"id": 42, "type": "private"},
        "photo": [{"file_id": "f", "file_unique_id": "u", "width": 1, "height": 1}],
        "reply_to_message": {"message_id": 6, "date": 1,
                             "chat": {"id": 42, "type": "private"}, "text": "earlier"},
        "forward_origin": {"type": "hidden_user", "date": 1, "sender_user_name": "Source"},
    }

    def respond(request):
        method = request.url.path.rsplit("/", 1)[-1]
        requests.append((method, request.content))
        results = {
            "getMe": {"id": 1, "is_bot": True, "first_name": "Test"},
            "getUpdates": [{"update_id": 10, "message": message}],
            "getFile": {"file_id": "f", "file_unique_id": "u", "file_path": "photos/f.jpg"},
            "sendMessage": message,
            "sendDocument": message,
        }
        return httpx.Response(200, json={"ok": True, "result": results[method]})

    request = HTTPXRequest(httpx_kwargs={"transport": httpx.MockTransport(respond)})
    client = Bot("123456:TEST-TOKEN", request=request, get_updates_request=request)
    transport = TelegramBot("123456:TEST-TOKEN", client=client)
    try:
        await transport.initialize()
        updates = await transport.get_updates(10)
        data = updates[0]["message"]
        assert data["from"]["id"] == 42
        assert data["photo"][0]["file_id"] == "f"
        assert data["reply_to_message"]["text"] == "earlier"
        assert data["forward_origin"]["sender_user_name"] == "Source"
        file = await transport.get_file("f")
        assert file["file_path"].endswith("/file/bot123456:TEST-TOKEN/photos/f.jpg")
        assert await transport.send_message(42, "**bold**") == 7
        body = next(body for method, body in requests if method == "sendMessage")
        payload = parse_qs(body.decode())
        assert payload["text"] == ["bold"]
        assert json.loads(payload["entities"][0]) == [{"type": "bold", "offset": 0, "length": 4}]
        with Stream(b"x" * 150_000) as stream:
            assert await transport.send_document(42, stream, "large.txt") == 7
            assert len(reads) >= 3
    finally:
        await transport.close()
    assert request._client.is_closed


async def test_retry_after_and_download_errors_do_not_expose_token(monkeypatch):
    from telegram.error import RetryAfter

    monkeypatch.setenv("PTB_TIMEDELTA", "1")
    client = FakeClient()
    client.script("get_updates", RetryAfter(9))
    with pytest.raises(TelegramError) as error:
        await bot(client).get_updates(0)
    assert error.value.retry_after == 9
    client.script("get_file", {"file_path": "https://api.telegram.org/file/botTESTTOKEN/f"})
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(
        lambda request: httpx2.Response(404)
    )) as http:
        transport = bot(client)
        transport.http = SimpleNamespace(client=http)
        with pytest.raises(TelegramError) as error:
            async for _ in transport.download("f"):
                pass
    assert "TESTTOKEN" not in str(error.value)
