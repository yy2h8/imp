"""TelegramBot over aiogram: entity-based sends with plain-text fallbacks.

The client is injectable (`client=None` builds a real aiogram Bot) so tests
drive a fake exposing the aiogram method surface.
"""

from __future__ import annotations

import io

import pytest
from aiogram.exceptions import TelegramBadRequest

from assistant.adapters.telegram import TelegramBot, TelegramError


class FakeClient:
    """Records aiogram-style calls; scriptable per-method results/errors."""

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

    async def download(self, file, destination=None, **kwargs):
        return await self._record("download", file=file, destination=destination)

    async def download_file(self, file_path, destination=None, **kwargs):
        return await self._record(
            "download_file", file_path=file_path, destination=destination
        )


class Msg:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


def bad_request(message: str = "can't parse entities") -> TelegramBadRequest:
    return TelegramBadRequest(method="sendMessage", message=message)


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
    from types import SimpleNamespace

    class UpdateObj(SimpleNamespace):
        def model_dump(self, **_):
            return self.__dict__.copy()

    client = FakeClient()
    client.script("get_me", {"id": 42, "username": "my_bot"})
    client.script("get_updates", [UpdateObj(update_id=41)])
    assert await bot(client).get_me() == {"id": 42, "username": "my_bot"}
    assert await bot(client).get_updates(40) == [{"update_id": 41}]


async def test_send_document_truncates_caption():
    client = FakeClient()
    client.script("send_document", Msg(3))
    assert await bot(client).send_document(1, b"data", "f.txt", "c" * 2000) == 3
    (kwargs,) = kinds(client, "send_document")
    assert len(kwargs["caption"].encode("utf-16-le")) // 2 <= 1024
    assert kwargs["document"].filename == "f.txt"


async def test_download_enforces_byte_limit():
    client = FakeClient()
    client.script("get_file", {"file_id": "f1", "file_path": "docs/f1.txt"})
    client.script("download", io.BytesIO(b"x" * 100))
    with pytest.raises(TelegramError, match="limit"):
        await bot(client, max_bytes=16).download("f1")


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
