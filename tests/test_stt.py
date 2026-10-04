"""SttClient: transcription through the shared AsyncOpenAI client."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from assistant.adapters.stt import SttClient
from imp.adapters import FileSystemAdapter


class StubTranscriptions:
    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []
        self.data: list[bytes] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.data.append(kwargs["file"][1].read(64 * 1024))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(script: list) -> tuple[SttClient, StubTranscriptions]:
    stub = StubTranscriptions(script)
    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=stub))
    stt = SttClient(client, model="openai/whisper-large-v3-turbo")
    return stt, stub


async def test_transcribe_returns_text_and_sends_model(tmp_path):
    stt, stub = make_client([SimpleNamespace(text="  hello there  ")])
    audio = tmp_path / "voice-1.ogg"
    audio.write_bytes(b"OggS-fake")

    assert await stt.transcribe(audio) == "hello there"
    call = stub.calls[0]
    assert call["model"] == "openai/whisper-large-v3-turbo"
    name, stream = call["file"]
    assert name == "voice-1.ogg" and stub.data == [b"OggS-fake"]
    assert stream.closed


async def test_transcribe_failure_returns_error_text_not_raise(tmp_path):
    stt, stub = make_client([RuntimeError("provider down")])
    audio = tmp_path / "voice-2.ogg"
    audio.write_bytes(b"OggS-fake")

    result = await stt.transcribe(audio)

    assert result.startswith("STT failed")
    assert "provider down" in result
    assert audio.exists()  # the caller decides what happens to the file
    assert stub.calls[0]["file"][1].closed


async def test_transcribe_empty_text_becomes_stt_failed(tmp_path):
    stt, _ = make_client([SimpleNamespace(text="  ")])
    (tmp_path / "v.ogg").write_bytes(b"x")
    assert (await stt.transcribe(Path(tmp_path / "v.ogg"))).startswith("STT failed")


async def test_transcribe_uses_injected_filesystem_byte_limit(tmp_path):
    stt, stub = make_client([SimpleNamespace(text="should not be sent")])
    stt.fs = FileSystemAdapter(tmp_path, max_bytes=2)
    audio = tmp_path / "large.ogg"
    audio.write_bytes(b"OggS-fake")
    assert "byte limit" in await stt.transcribe(audio)
    assert stub.calls == []


async def test_transcribe_accepts_relative_audio_path(tmp_path, monkeypatch):
    stt, _ = make_client([SimpleNamespace(text="relative audio")])
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "voice.ogg").write_bytes(b"OggS-fake")
    monkeypatch.chdir(tmp_path)
    assert await stt.transcribe(Path("inbox/voice.ogg")) == "relative audio"


async def test_transcribe_cancelled_while_opening_closes_file(tmp_path, monkeypatch):
    stt, _ = make_client([SimpleNamespace(text="should not be sent")])
    fs = stt.fs = FileSystemAdapter(tmp_path)
    audio = tmp_path / "voice.ogg"
    audio.write_bytes(b"OggS-fake")
    entered, release = asyncio.Event(), threading.Event()
    loop, original_open = asyncio.get_running_loop(), fs.open_read
    handles = []

    def delayed_open(path):
        stream = original_open(path)
        handles.append(stream)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return stream

    monkeypatch.setattr(fs, "open_read", delayed_open)
    task = asyncio.create_task(stt.transcribe(audio))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert handles[0].closed
