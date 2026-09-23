"""SttClient: transcription through the shared AsyncOpenAI client."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from assistant.adapters.stt import SttClient


class StubTranscriptions:
    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
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
    name, data = call["file"]
    assert name == "voice-1.ogg" and data == b"OggS-fake"


async def test_transcribe_failure_returns_error_text_not_raise(tmp_path):
    stt, _ = make_client([RuntimeError("provider down")])
    audio = tmp_path / "voice-2.ogg"
    audio.write_bytes(b"OggS-fake")

    result = await stt.transcribe(audio)

    assert result.startswith("STT failed")
    assert "provider down" in result
    assert audio.exists()  # the caller decides what happens to the file


async def test_transcribe_empty_text_becomes_stt_failed(tmp_path):
    stt, _ = make_client([SimpleNamespace(text="  ")])
    (tmp_path / "v.ogg").write_bytes(b"x")
    assert (await stt.transcribe(Path(tmp_path / "v.ogg"))).startswith("STT failed")
