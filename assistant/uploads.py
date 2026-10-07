"""Download all owner-shared Telegram media into inbox/ and synthesize turns."""

from __future__ import annotations

import asyncio
import mimetypes
import re
from contextlib import aclosing
from pathlib import Path

from imp.adapters import FileSystemAdapter

from .adapters.stt import SttClient
from .adapters.telegram import send_text

_EXTENSIONS = {
    "photo": ".jpg",
    "video": ".mp4",
    "audio": ".m4a",
    "voice": ".ogg",
    "video_note": ".mp4",
    "animation": ".mp4",
    "sticker": ".webp",
}


def _size_text(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    if size >= 1_000:
        return f"{size / 1_000:.1f} kB"
    return f"{size} B"


class Uploads:
    """Saves owner uploads into the workspace inbox; transport injectable."""

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

    async def handle(
        self,
        attachments: list[dict],
        caption: str = "",
        reply_context: str = "",
    ) -> str | None:
        """Save every attachment; captions and available audio transcripts
        become one prompt. Uncaptioned non-audio files are saved and acked."""
        saved: list[tuple[dict, Path]] = []
        transcripts: list[str] = []
        for item in attachments:
            name = self._filename(item)
            path = await self._save(item.get("file_id"), name)
            if path is None:
                continue
            saved.append((item, path))
            if item.get("kind") in {"voice", "audio"} and self.stt is not None:
                await self.bot.send_chat_action(self.chat_id, "typing")
                transcript = await self.stt.transcribe(path)
                if transcript.startswith("STT failed"):
                    await send_text(
                        self.bot,
                        self.chat_id,
                        f"*ошибка:* {transcript} — аудио сохранено в {self._rel(path)}.",
                    )
                else:
                    transcripts.append(transcript)
                    if item.get("kind") == "voice":
                        try:
                            path.unlink(missing_ok=True)
                        except OSError:
                            pass  # a failed cleanup must not kill the queued turn

        if not saved:
            return None
        caption = caption.strip() or next(
            (item.get("caption", "").strip() for item, _ in saved if item.get("caption")),
            "",
        )
        if not caption and not transcripts:
            if len(saved) == 1:
                await self._ack(saved[0][1])
            else:
                names = ", ".join(self._rel(path) for _, path in saved)
                await send_text(self.bot, self.chat_id, f"Сохранено: {names}.")
            return None

        details = ", ".join(self._rel(path) for _, path in saved)
        forwards = sorted(
            {item["forward_origin"] for item, _ in saved if item.get("forward_origin")}
        )
        source = f" forwarded from {', '.join(forwards)}" if forwards else ""
        parts = [f"Owner sent{source} file(s) saved to {details}."]
        if reply_context:
            parts.append(f"The owner is replying to: {reply_context}")
        if caption:
            parts.append(f"Note: {caption}")
        if transcripts:
            parts.append("Audio transcription: " + "\n".join(transcripts))
        return "\n".join(parts)

    def _filename(self, item: dict) -> str:
        kind = str(item.get("kind") or "file")
        name = str(item.get("file_name") or "")
        if not name or name != Path(name).name:
            suffix = Path(name).suffix if name else _EXTENSIONS.get(kind, "")
            mime_type = str(item.get("mime_type") or "")
            if not suffix and mime_type == "application/x-tgsticker":
                suffix = ".tgs"
            elif not suffix and mime_type:
                suffix = mimetypes.guess_extension(mime_type) or ""
            unique = item.get("file_unique_id") or item.get("file_id") or "unknown"
            name = f"{kind}-{unique}{suffix}"
        name = re.sub(r"[^\w. -]", "_", name)[:200]
        return name if name not in {".", "..", ""} else f"{kind}-file"

    async def _save(self, file_id: str | None, name: str) -> Path | None:
        if not file_id:
            await send_text(self.bot, self.chat_id, "Не удалось сохранить вложение: отсутствует file id.")
            return None
        try:
            while True:
                target = await asyncio.to_thread(self._target, name)
                try:
                    async with aclosing(self.bot.download(file_id)) as chunks:
                        return await self.fs.create_stream(target, chunks)
                except FileExistsError:
                    continue
        except Exception as exc:
            await send_text(
                self.bot, self.chat_id, f"*ошибка:* не удалось сохранить файл: {exc}"
            )
            return None

    def _target(self, name: str) -> Path:
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
            f"Сохранён {self._rel(saved)} ({_size_text(saved.stat().st_size)}).",
        )
