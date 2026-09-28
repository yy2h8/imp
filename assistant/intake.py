"""Pure Telegram-message routing, album coalescing and durable FIFO intake."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .db import BUSY_TIMEOUT_MS

_FILE_KINDS = (
    "document", "photo", "video", "audio", "voice", "video_note",
    "animation", "sticker",
)
_STRUCTURED_KINDS = (
    "contact", "location", "venue", "poll", "dice", "game",
    "web_app_data", "paid_media",
)
_METADATA_KEYS = {
    "message_id", "from", "from_user", "chat", "date", "edit_date",
    "media_group_id", "forward_origin", "forward_from", "forward_from_chat",
    "forward_date", "caption", "caption_entities", "entities",
}


@dataclass(slots=True, frozen=True)
class Routed:
    command: str | None = None
    answer: str | None = None
    text: str | None = None
    attachment: dict | None = None
    ignore: bool = False


def _forward_source(origin: Any) -> str | None:
    if not isinstance(origin, dict):
        return str(origin) if origin else None
    sender = origin.get("sender_user") or origin.get("sender_user_name")
    chat = origin.get("sender_chat") or {}
    source = (
        sender.get("first_name") or sender.get("username") or ""
        if isinstance(sender, dict)
        else str(sender or "")
    )
    source = source or (chat.get("title", "") if isinstance(chat, dict) else "")
    return source or str(origin.get("type") or "unknown source")


def normalize_attachment(message: dict) -> dict | None:
    """Normalize one Telegram file-bearing message into a small portable dict."""
    kind = next((name for name in _FILE_KINDS if message.get(name) is not None), None)
    if kind is None:
        return None
    item = message[kind]
    if kind == "photo":
        if not isinstance(item, list) or not item:
            return None
        item = max(item, key=lambda p: p.get("file_size", 0))
    if not isinstance(item, dict) or not item.get("file_id"):
        return None
    return {
        "kind": kind,
        "file_id": str(item["file_id"]),
        "file_unique_id": str(item.get("file_unique_id") or ""),
        "file_name": str(item.get("file_name") or ""),
        "mime_type": str(item.get("mime_type") or ""),
        "caption": str(message.get("caption") or ""),
        "message_id": message.get("message_id"),
        "media_group_id": message.get("media_group_id"),
        "forward_origin": _forward_source(message.get("forward_origin")),
    }


def _command(text: str) -> str:
    return text.split()[0].lower() if text.split() else ""


def _structured_text(message: dict) -> str | None:
    for kind in _STRUCTURED_KINDS:
        value = message.get(kind)
        if value is not None:
            return f"Owner shared {kind.replace('_', ' ')}: {json.dumps(value, ensure_ascii=False, default=str)}"
    # Preserve a future Bot API content kind as text rather than silently drop it.
    fields = {
        key: value for key, value in message.items()
        if key not in _METADATA_KEYS and key not in _FILE_KINDS and value is not None
    }
    if fields:
        key, value = next(iter(fields.items()))
        return f"Owner shared {key.replace('_', ' ')}: {json.dumps(value, ensure_ascii=False, default=str)}"
    return None


def route_message(
    message: dict,
    *,
    owner_id: int,
    question_pending: bool,
    busy: bool,
) -> Routed:
    """Validate owner-private-chat updates and route text/answers/files."""
    sender = message.get("from") or message.get("from_user") or {}
    chat = message.get("chat") or {}
    if (
        sender.get("id") != owner_id
        or chat.get("id") != owner_id
        or chat.get("type") != "private"
    ):
        return Routed(ignore=True)
    text = str(message.get("text") or "").strip()
    if text:
        command = _command(text)
        if question_pending and command not in {"/new", "/status"}:
            return Routed(answer=text)
        return Routed(command=command if command in {"/new", "/status"} else None, text=text)
    attachment = normalize_attachment(message)
    if attachment is not None:
        return Routed(attachment=attachment)
    if any(message.get(kind) is not None for kind in _FILE_KINDS):
        return Routed(
            text="Owner sent an attachment I could not identify; ask them to resend it."
        )
    structured = _structured_text(message)
    if structured is not None:
        return Routed(text=structured)
    return Routed(ignore=True)


class AlbumBuffer:
    """Collect separate Telegram media-group updates until a quiet window."""

    def __init__(self, wait_s: float = 1.5) -> None:
        self.wait_s = wait_s
        self._items: dict[str, list[dict]] = {}
        self._deadlines: dict[str, float] = {}
        self._seen_ids: set[tuple[str, int]] = set()

    async def add(self, message: dict) -> None:
        group_id = message.get("media_group_id")
        if group_id is None:
            return
        key = str(group_id)
        message_id = message.get("message_id")
        marker = (key, message_id) if type(message_id) is int else None
        if marker is not None and marker in self._seen_ids:
            return
        if marker is not None:
            self._seen_ids.add(marker)
        self._items.setdefault(key, []).append(message)
        self._deadlines[key] = time.monotonic() + self.wait_s

    async def flush_due(self) -> list[list[dict]]:
        now = time.monotonic()
        due = [key for key, deadline in self._deadlines.items() if deadline <= now]
        groups = [self._items.pop(key) for key in due]
        for key in due:
            self._deadlines.pop(key, None)
            self._seen_ids = {marker for marker in self._seen_ids if marker[0] != key}
        return groups

    async def flush_all(self) -> list[list[dict]]:
        groups = list(self._items.values())
        self._items.clear()
        self._deadlines.clear()
        self._seen_ids.clear()
        return groups


def queued_notice(depth: int) -> str:
    ahead = max(depth - 1, 0)
    if ahead == 0:
        return "Принято — в очереди."
    return f"Принято — в очереди (перед вами ещё {ahead})."


class Intake:
    """Deduplicate owner message IDs and persist accepted work to queue."""

    def __init__(
        self,
        db: aiosqlite.Connection,
        bot,
        chat_id: int,
        is_busy=None,
        db_path: Path | None = None,
    ) -> None:
        self.db = db
        self.bot = bot
        self.chat_id = chat_id
        self.is_busy = is_busy or (lambda: False)
        self.db_path = db_path
        self._lock = asyncio.Lock()

    async def mark_seen(self, message_id: int | None) -> bool:
        if type(message_id) is not int:
            return True
        async with self._lock:
            path = await self._path()
            return await asyncio.to_thread(_mark_seen_sync, path, message_id)

    async def _path(self) -> Path:
        if self.db_path is not None:
            return self.db_path
        rows = await self.db.execute_fetchall("PRAGMA database_list")
        if not rows or not rows[0][2]:
            raise ValueError("Intake requires a file-backed state.db")
        self.db_path = Path(rows[0][2])
        return self.db_path

    async def accept(self, message: dict) -> str | None:
        """Queue one normalized text or attachment message; return an ack."""
        return await self._accept(message, deduplicate=True)

    async def accept_unseen(self, message: dict) -> str | None:
        """Queue a message whose ID was already claimed during question routing."""
        return await self._accept(message, deduplicate=False)

    async def _accept(self, message: dict, *, deduplicate: bool) -> str | None:
        attachment = normalize_attachment(message)
        text = str(message.get("text") or "").strip()
        if attachment is not None:
            payload = {"attachments": [attachment], "caption": attachment["caption"]}
            kind = "attachment"
        else:
            text = text or _structured_text(message) or ""
            if not text:
                return None
            payload = {"text": text}
            kind = "text"
        message_id = message.get("message_id")
        async with self._lock:
            accepted, depth = await asyncio.to_thread(
                _enqueue_sync,
                await self._path(),
                kind,
                json.dumps(payload, ensure_ascii=False),
                [message_id] if type(message_id) is int else [],
                deduplicate,
            )
        if not accepted:
            return None
        depth_before = depth - 1
        return queued_notice(depth) if self.is_busy() or depth_before else None

    async def accept_album(self, messages: list[dict]) -> str | None:
        """Queue one entry containing all items from a buffered album."""
        attachments = [
            attachment for message in messages
            if (attachment := normalize_attachment(message)) is not None
        ]
        if not attachments:
            return None
        caption = next((item["caption"] for item in attachments if item["caption"]), "")
        ids = [m.get("message_id") for m in messages if type(m.get("message_id")) is int]
        async with self._lock:
            accepted, depth = await asyncio.to_thread(
                _enqueue_sync,
                await self._path(),
                "attachment",
                json.dumps({"attachments": attachments, "caption": caption}, ensure_ascii=False),
                ids,
                True,
            )
        if not accepted:
            return None
        depth_before = depth - 1
        return queued_notice(depth) if self.is_busy() or depth_before else None


def _mark_seen_sync(path: Path, message_id: int) -> bool:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'last_message_id'"
        ).fetchone()
        last = json.loads(row[0]) if row else None
        if last is not None and message_id <= int(last):
            conn.rollback()
            return False
        conn.execute(
            "INSERT INTO kv(key, value) VALUES ('last_message_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(message_id),),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def _enqueue_sync(
    path: Path, kind: str, payload: str, message_ids: list[int], deduplicate: bool
) -> tuple[bool, int]:
    """Atomically deduplicate and persist an accepted message/album."""
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value FROM kv WHERE key = 'last_message_id'"
        ).fetchone()
        last = int(json.loads(row[0])) if row else None
        newest = max(message_ids) if message_ids else None
        if deduplicate and newest is not None and last is not None and newest <= last:
            conn.rollback()
            return False, 0
        conn.execute(
            "INSERT INTO queue(kind, payload, state, created_at) "
            "VALUES (?, ?, 'waiting', ?)",
            (kind, payload, datetime.now(UTC).isoformat()),
        )
        if newest is not None and (last is None or newest > last):
            conn.execute(
                "INSERT INTO kv(key, value) VALUES ('last_message_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (json.dumps(newest),),
            )
        (depth,) = conn.execute(
            "SELECT COUNT(*) FROM queue WHERE state = 'waiting'"
        ).fetchone()
        conn.commit()
        return True, depth
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()
