"""Owner attachment saving, m4a/audio handling, forwards and voice STT."""

from __future__ import annotations

from pathlib import Path

from assistant.uploads import Uploads


class StubBot:
    def __init__(self) -> None:
        self.files = {f"id-{kind}": f"bytes-{kind}".encode() for kind in (
            "document", "photo", "video", "audio", "voice", "video_note", "animation", "sticker"
        )}
        self.files["second"] = b"second-photo"
        self.sent: list[str] = []
        self.actions: list[str] = []

    async def download(self, file_id: str) -> bytes:
        return self.files[file_id]

    async def send_text(self, chat_id: int, text: str) -> list[int]:
        self.sent.append(text)
        return [len(self.sent)]

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append(action)


class StubStt:
    def __init__(self, result: str = "transcribed words") -> None:
        self.result = result
        self.paths: list[Path] = []

    async def transcribe(self, path: Path) -> str:
        self.paths.append(path)
        return self.result


def attachment(kind: str, file_id: str | None = None, **fields) -> dict:
    return {
        "kind": kind,
        "file_id": file_id or f"id-{kind}",
        "file_name": fields.pop("file_name", ""),
        "file_unique_id": fields.pop("file_unique_id", f"unique-{kind}"),
        **fields,
    }


def make_uploads(tmp_path: Path, stt=None):
    bot = StubBot()
    uploads = Uploads(bot=bot, inbox=tmp_path / "inbox", stt=stt, chat_id=7)
    return uploads, bot


async def test_all_common_media_kinds_save_and_caption_becomes_prompt(tmp_path):
    uploads, _bot = make_uploads(tmp_path)
    attachments = [
        attachment("document", file_name="report.pdf"),
        attachment("photo"),
        attachment("video"),
        attachment("audio", file_name="recording.m4a", mime_type="audio/mp4"),
        attachment("video_note"),
        attachment("animation"),
        attachment("sticker"),
    ]
    prompt = await uploads.handle(attachments, caption="review these")
    assert prompt is not None and "review these" in prompt
    assert all((tmp_path / "inbox").iterdir())
    assert "recording.m4a" in prompt
    assert len(list((tmp_path / "inbox").iterdir())) == len(attachments)


async def test_m4a_as_audio_and_as_document_both_save(tmp_path):
    uploads, _ = make_uploads(tmp_path)
    audio = await uploads.handle(
        [attachment("audio", file_name="memo.m4a", mime_type="audio/mp4")],
        caption="listen to this",
    )
    document = await uploads.handle(
        [attachment("document", file_name="shared.m4a")], caption="and this"
    )
    assert "memo.m4a" in audio and "shared.m4a" in document
    assert (tmp_path / "inbox" / "memo.m4a").exists()
    assert (tmp_path / "inbox" / "shared.m4a").exists()


async def test_forward_origin_is_in_prompt(tmp_path):
    uploads, _ = make_uploads(tmp_path)
    prompt = await uploads.handle(
        [attachment("document", file_name="notes.txt", forward_origin="a channel")],
        caption="check this",
    )
    assert "forwarded" in prompt and "a channel" in prompt


async def test_reply_context_is_in_attachment_prompt(tmp_path):
    uploads, _ = make_uploads(tmp_path)
    prompt = await uploads.handle(
        [attachment("document", file_name="notes.txt")],
        caption="please check",
        reply_context="Does this file answer the question?",
    )
    assert "Does this file answer the question?" in prompt
    assert "please check" in prompt


async def test_album_saves_all_files_in_one_prompt(tmp_path):
    uploads, _ = make_uploads(tmp_path)
    prompt = await uploads.handle(
        [attachment("photo", file_id="id-photo"), attachment("photo", file_id="second")],
        caption="compare these",
    )
    assert "compare these" in prompt
    assert len(list((tmp_path / "inbox").iterdir())) == 2


async def test_voice_note_transcribes_then_removes_audio(tmp_path):
    stt = StubStt()
    uploads, bot = make_uploads(tmp_path, stt)
    prompt = await uploads.handle(
        [attachment("voice", file_id="id-voice", message_id=42)], caption=""
    )
    assert "transcribed words" in prompt
    assert len(stt.paths) == 1
    assert not stt.paths[0].exists()  # voice audio deleted on success
    assert bot.actions == ["typing"]


async def test_audio_m4a_transcribes_but_keeps_original(tmp_path):
    stt = StubStt()
    uploads, _ = make_uploads(tmp_path, stt)
    prompt = await uploads.handle(
        [attachment("audio", file_name="memo.m4a")], caption=""
    )
    assert "transcribed words" in prompt
    assert stt.paths[0].exists()  # shared audio remains available in inbox


async def test_uncaptioned_document_is_saved_and_acknowledged(tmp_path):
    uploads, bot = make_uploads(tmp_path)
    prompt = await uploads.handle(
        [attachment("document", file_name="quiet.txt")], caption=""
    )
    assert prompt is None
    assert (tmp_path / "inbox" / "quiet.txt").exists()
    assert bot.sent and "Saved inbox/quiet.txt" in bot.sent[0]


async def test_download_failure_is_reported_not_silently_dropped(tmp_path):
    uploads, bot = make_uploads(tmp_path)
    result = await uploads.handle(
        [attachment("document", file_id="missing", file_name="missing.txt")],
        caption="please inspect",
    )
    assert result is None
    assert bot.sent and "could not save" in bot.sent[0]
