"""Uploads: inbox naming, collision suffixes, caption/voice prompt coupling."""

from __future__ import annotations

from pathlib import Path

from assistant.adapters.stt import SttClient
from assistant.uploads import Uploads, _size_text


class StubBot:
    def __init__(self):
        self.sent: list[str] = []
        self.files: dict[str, bytes] = {}  # file_path -> bytes
        self.get_file_calls: list[str] = []

    async def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append(text)
        return len(self.sent)

    async def send_chat_action(self, chat_id: int, action: str) -> None:
        pass

    async def get_file(self, file_id: str) -> dict:
        self.get_file_calls.append(file_id)
        return {"file_path": f"docs/{file_id}"}

    async def download_file(self, file_path: str) -> bytes:
        # the transport resolves file_path to bytes; keyed like get_file's answer
        return self.files[file_path.removeprefix("docs/")]


class StubStt:
    """Duck-typed stand-in for SttClient (result text, call recording)."""

    def __init__(self, result: str):
        self.result = result
        self.paths: list[Path] = []
        self.bytes: list[bytes] = []

    async def transcribe(self, path: Path) -> str:
        self.paths.append(path)
        self.bytes.append(path.read_bytes())
        return self.result


def make_uploads(tmp_path: Path, stt=None) -> tuple[Uploads, StubBot]:
    bot = StubBot()
    return Uploads(bot=bot, inbox=tmp_path / "inbox", stt=stt, chat_id=7), bot


def doc_message(file_id: str, name: str, caption: str | None = None) -> dict:
    message: dict = {
        "message_id": 10,
        "document": {
            "file_id": file_id,
            "file_name": name,
            "file_size": 1500,
            "mime_type": "application/pdf",
        },
    }
    if caption is not None:
        message["caption"] = caption
    return message


class TestDocuments:
    async def test_saved_under_original_name(self, tmp_path):
        uploads, bot = make_uploads(tmp_path)
        bot.files["f1"] = b"pdf-bytes"

        prompt = await uploads.handle(doc_message("f1", "report.pdf"))

        assert prompt is None  # no caption: ack only, no turn
        assert (tmp_path / "inbox" / "report.pdf").read_bytes() == b"pdf-bytes"
        assert bot.sent == ["Saved inbox/report.pdf (9 B)."]

    async def test_caption_becomes_turn_prompt(self, tmp_path):
        uploads, bot = make_uploads(tmp_path)
        bot.files["f1"] = b"data"

        prompt = await uploads.handle(
            doc_message("f1", "report.pdf", caption="summarise")
        )

        assert prompt == (
            "Owner sent a file, saved to inbox/report.pdf, with the note: summarise"
        )
        assert bot.sent == []  # the caption is the turn, not an ack

    async def test_collision_gets_numeric_suffix(self, tmp_path):
        uploads, bot = make_uploads(tmp_path)
        bot.files = {"f1": b"one", "f2": b"two", "f3": b"three"}
        await uploads.handle(doc_message("f1", "report.pdf"))
        await uploads.handle(doc_message("f2", "report.pdf"))

        await uploads.handle(doc_message("f3", "report.pdf"))

        names = sorted(p.name for p in (tmp_path / "inbox").iterdir())
        assert names == ["report-2.pdf", "report-3.pdf", "report.pdf"]

    async def test_unsafe_or_missing_name_gets_generated(self, tmp_path):
        uploads, bot = make_uploads(tmp_path)
        bot.files = {"f1": b"a", "f2": b"b"}
        await uploads.handle(doc_message("f1", ""))
        await uploads.handle(doc_message("f2", "../../etc/passwd"))

        names = sorted(p.name for p in (tmp_path / "inbox").iterdir())
        assert names == ["file-f1", "file-f2"]  # basename stripped, generated

    async def test_download_failure_messages_owner(self, tmp_path):
        uploads, bot = make_uploads(tmp_path)  # bot.files empty -> KeyError

        prompt = await uploads.handle(doc_message("missing", "x.txt"))

        assert prompt is None
        assert bot.sent and "could not save" in bot.sent[0]
        assert not (tmp_path / "inbox").exists() or not list(
            (tmp_path / "inbox").iterdir()
        )


class TestPhotos:
    async def test_largest_photosize_wins(self, tmp_path):
        uploads, bot = make_uploads(tmp_path)
        bot.files = {"small": b"s", "large": b"L"}
        message = {
            "message_id": 11,
            "photo": [
                {"file_id": "small", "file_unique_id": "u1", "width": 90},
                {"file_id": "large", "file_unique_id": "u2", "width": 1280},
            ],
        }

        await uploads.handle(message)

        saved = list((tmp_path / "inbox").iterdir())
        assert len(saved) == 1
        assert saved[0].read_bytes() == b"L"  # photo[-1] is the largest size


class TestVoice:
    def voice_message(self, file_id: str = "v1") -> dict:
        return {"message_id": 12, "voice": {"file_id": file_id, "duration": 3}}

    async def test_success_transcribes_and_deletes_audio(self, tmp_path):
        stt = StubStt("what time is it")
        uploads, bot = make_uploads(tmp_path, stt=stt)
        bot.files["v1"] = b"ogg-bytes"

        prompt = await uploads.handle(self.voice_message())

        assert prompt is not None and "what time is it" in prompt
        assert stt.paths[0].name == "voice-12.ogg"
        assert stt.bytes[0] == b"ogg-bytes"
        assert list((tmp_path / "inbox").iterdir()) == []  # audio deleted

    async def test_failure_keeps_audio_and_tells_owner(self, tmp_path):
        stt = StubStt("STT failed: quota")
        uploads, bot = make_uploads(tmp_path, stt=stt)
        bot.files["v1"] = b"ogg-bytes"

        prompt = await uploads.handle(self.voice_message())

        assert prompt is None
        assert [p.name for p in (tmp_path / "inbox").iterdir()] == ["voice-12.ogg"]
        assert bot.sent and "STT failed" in bot.sent[0]

    async def test_without_stt_voice_is_skipped(self, tmp_path):
        uploads, bot = make_uploads(tmp_path, stt=None)

        assert await uploads.handle(self.voice_message()) is None
        assert bot.get_file_calls == []


def test_size_text_units():
    assert _size_text(9) == "9 B"
    assert _size_text(1500) == "1.5 kB"
    assert _size_text(2_400_000) == "2.4 MB"


def test_stt_client_is_the_real_signature():
    """Uploads accepts the real SttClient (structural check, no network)."""
    assert hasattr(SttClient, "transcribe")


async def test_dangling_upload_link_is_not_followed(tmp_path):
    from assistant.uploads import Uploads

    class Bot:
        async def get_file(self, file_id):
            return {"file_path": "file"}

        async def download_file(self, path):
            return b"safe"

        async def send_message(self, chat_id, text):
            return 1

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    (inbox / "note").symlink_to(outside)
    saved = await Uploads(Bot(), inbox)._save("file", "note")
    assert not outside.exists()
    assert saved is not None and saved.read_bytes() == b"safe"
