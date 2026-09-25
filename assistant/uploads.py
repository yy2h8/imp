"""Owner uploads: documents, photos, and voice notes landing in inbox/.

Downloaded via the Bot API's getFile/download_file (20 MB platform cap),
saved under a sanitized original name with -2/-3... collision suffixes. Voice
notes go through STT and become the next prompt — the audio file is deleted on
success and kept on failure. A caption on a document/photo becomes the prompt
for a new turn; without one the save is only acknowledged.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from imp.adapters import FileSystemAdapter

from .adapters.stt import SttClient
from .adapters.telegram import send_text

VOICE_PREFIX = "voice-"


def _size_text(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} kB"
    return f"{size} B"


class Uploads:
    """Saves owner uploads into the workspace; bot injectable for tests."""

    def __init__(
        self,
        bot,
        inbox: Path,
        stt: SttClient | None = None,
        chat_id: int = 0,
        fs: FileSystemAdapter | None = None,
    ) -> None:
        self.bot = bot
        self.fs = fs or FileSystemAdapter(inbox.parent)
        self.inbox = self.fs.resolve_path(inbox)
        self.stt = stt
        self.chat_id = chat_id
        self.inbox.mkdir(parents=True, exist_ok=True)

    async def handle(self, message: dict) -> str | None:
        """Route one incoming message's attachment.

        Returns the prompt for a new agent turn, or None when the attachment
        was only saved and acknowledged (or the message had none)."""
        voice = message.get("voice")
        if voice is not None:
            return await self._voice(message, voice)
        document = message.get("document")
        if document is not None:
            return await self._document(message, document, prefix="")
        photo = message.get("photo")
        if photo is not None:
            return await self._document(message, photo[-1], prefix="photo-")
        return None

    async def _document(self, message: dict, item: dict, prefix: str) -> str | None:
        caption = (message.get("caption") or "").strip()
        name = str(item.get("file_name") or "")
        if not name or name != Path(name).name:
            unique = item.get("file_unique_id")
            name = f"{prefix or 'file'}-{unique or item.get('file_id', 'unknown')}"
        name = re.sub(r"[^\w. -]", "_", name)[:200]
        if name in {".", "..", ""}:
            name = "file"
        saved = await self._save(item.get("file_id"), name)
        if saved is None:
            return None
        if not caption:
            await self._ack(saved)
            return None
        return (
            f"Owner sent a file, saved to {self._rel(saved)}, with the note: {caption}"
        )

    async def _voice(self, message: dict, voice: dict) -> str | None:
        if self.stt is None:
            return None  # STT unavailable: silently skip (voice is optional)
        saved = await self._save(
            voice.get("file_id"),
            f"{VOICE_PREFIX}{message.get('message_id', 'note')}.ogg",
        )
        if saved is None:
            return None
        await self.bot.send_chat_action(self.chat_id, "typing")
        transcript = await self.stt.transcribe(saved)
        if transcript.startswith("STT failed"):
            await send_text(
                self.bot,
                self.chat_id,
                f"*error:* {transcript} — the audio is kept at {self._rel(saved)}.",
            )
            return None
        try:
            saved.unlink(missing_ok=True)  # success: the audio has served its purpose
        except OSError:
            pass  # a file that would not delete must never kill the poll loop
        return (
            "Owner sent a voice note (the audio file is deleted after "
            f"transcription), saying: {transcript}"
        )

    async def _save(self, file_id, name: str) -> Path | None:
        """Download and store one attachment; None (plus owner notice) on failure."""
        if not file_id:
            return None
        try:
            entry = await self.bot.get_file(file_id)
            data = await self.bot.download_file(entry["file_path"])
            return await asyncio.to_thread(self._create, name, data)
        except Exception as exc:
            await send_text(
                self.bot, self.chat_id, f"*error:* could not save the file: {exc}"
            )
            return None

    def _create(self, name: str, data: bytes) -> Path:
        while True:
            target = self._target(name)
            try:
                return self.fs.create_bytes(target, data)
            except FileExistsError:
                continue

    def _target(self, name: str) -> Path:
        """inbox/<name>, numeric suffix on collision."""
        candidate, n = self.inbox / name, 1
        while candidate.exists() or candidate.is_symlink():
            n += 1
            stem, suffix = Path(name).stem, Path(name).suffix
            candidate = self.inbox / f"{stem}-{n}{suffix}"
        return candidate

    def _rel(self, path: Path) -> str:
        return f"inbox/{path.name}"

    async def _ack(self, saved: Path) -> None:
        await send_text(
            self.bot,
            self.chat_id,
            f"Saved {self._rel(saved)} ({_size_text(saved.stat().st_size)}).",
        )
