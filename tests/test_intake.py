"""Telegram message normalization, routing, durable intake and albums."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from assistant.db import (
    STATE_DB_NAME,
    kv_get,
    open_db,
    queue_claim_next,
    queue_count_waiting,
)
from assistant.intake import AlbumBuffer, Intake, normalize_attachment, route_message


def message(**fields):
    return {
        "message_id": fields.pop("message_id", 1),
        "from": {"id": 7},
        "chat": {"id": 7, "type": "private"},
        **fields,
    }


def test_normalize_common_file_kinds_and_m4a():
    for kind in ("document", "video", "audio", "voice", "video_note", "animation", "sticker"):
        item = {"file_id": f"id-{kind}", "file_unique_id": f"uniq-{kind}"}
        if kind == "audio":
            item.update(file_name="recording.m4a", mime_type="audio/mp4")
        normalized = normalize_attachment(message(**{kind: item}))
        assert normalized["kind"] == kind
        assert normalized["file_id"] == f"id-{kind}"
    audio = normalize_attachment(message(audio={"file_id": "a", "file_name": "voice.m4a"}))
    doc = normalize_attachment(message(document={"file_id": "d", "file_name": "voice.m4a"}))
    assert audio["kind"] == "audio" and doc["kind"] == "document"


def test_photo_uses_largest_size_and_keeps_forward_origin():
    photo = [
        {"file_id": "small", "file_unique_id": "s", "file_size": 10},
        {"file_id": "large", "file_unique_id": "l", "file_size": 100},
    ]
    forwarded = normalize_attachment(
        message(photo=photo, forward_origin={"type": "user", "sender_user": {"first_name": "Ada"}})
    )
    assert forwarded["file_id"] == "large"
    assert "Ada" in forwarded["forward_origin"]


def test_route_owner_private_text_question_command_and_other_sender():
    routed = route_message(message(text="hello"), owner_id=7, question_pending=False, busy=False)
    assert routed.text == "hello" and routed.ignore is False
    answer = route_message(message(text="answer"), owner_id=7, question_pending=True, busy=True)
    assert answer.answer == "answer"
    command = route_message(message(text="/new"), owner_id=7, question_pending=True, busy=True)
    assert command.answer is None and command.text == "/new"
    stranger = route_message(message(text="no", **{"from": {"id": 9}}), owner_id=7, question_pending=False, busy=False)
    assert stranger.ignore
    group = message(text="no", chat={"id": 8, "type": "group"})
    assert route_message(group, owner_id=7, question_pending=False, busy=False).ignore


def test_reply_to_bot_message_carries_original_text_into_prompt():
    routed = route_message(
        message(
            text="use the second option",
            reply_to_message={"from": {"id": 999}, "text": "Which database should I use?"},
        ),
        owner_id=7,
        question_pending=False,
        busy=False,
    )
    assert "Which database should I use?" in routed.text
    assert "use the second option" in routed.text


def test_pending_ask_reply_stays_plain_answer():
    routed = route_message(
        message(
            text="yes",
            reply_to_message={"from": {"id": 999}, "text": "Should I continue?"},
        ),
        owner_id=7,
        question_pending=True,
        busy=True,
    )
    assert routed.answer == "yes"


def test_route_structured_owner_message_instead_of_silently_ignoring():
    routed = route_message(
        message(location={"latitude": 51.5, "longitude": -0.1}),
        owner_id=7,
        question_pending=False,
        busy=False,
    )
    assert not routed.ignore and "51.5" in routed.text and "-0.1" in routed.text


@pytest.fixture
async def db(tmp_path):
    conn = await open_db(tmp_path / STATE_DB_NAME)
    yield conn
    await conn.close()


class FakeBot:
    async def send_text(self, chat_id, text):
        return [1]


async def test_redelivered_update_not_duplicated(db, tmp_path):
    intake = Intake(db, FakeBot(), chat_id=7, db_path=tmp_path / STATE_DB_NAME)
    incoming = message(text="once", message_id=9)
    await intake.accept(incoming)
    await intake.accept(incoming)
    assert await queue_count_waiting(db) == 1


async def test_queued_attachment_keeps_reply_context(db, tmp_path):
    intake = Intake(db, FakeBot(), chat_id=7, db_path=tmp_path / STATE_DB_NAME)
    await intake.accept(
        message(
            document={"file_id": "doc", "file_name": "report.pdf"},
            caption="check this",
            reply_to_message={"text": "What did the report say?"},
        )
    )
    rows = await db.execute_fetchall("SELECT payload FROM queue")
    payload = json.loads(rows[0][0])
    assert payload["reply_context"] == "What did the report say?"


async def test_album_merge_interleaved_with_text(db, tmp_path):
    intake = Intake(db, FakeBot(), chat_id=7, db_path=tmp_path / STATE_DB_NAME)
    albums = AlbumBuffer(wait_s=0.03)
    first = message(photo=[{"file_id": "p1", "file_size": 1}], media_group_id="g", message_id=10)
    second = message(photo=[{"file_id": "p2", "file_size": 2}], media_group_id="g", message_id=13)
    await intake.accept_album_item(first)
    await albums.add(first)
    await intake.accept(message(text="between photos", message_id=11))
    await intake.accept_album_item(second)
    await albums.add(second)
    assert await queue_claim_next(db) is None  # earlier collecting album blocks later text
    await asyncio.sleep(0.04)
    groups = await albums.flush_due()
    assert groups == ["g"]
    await intake.finish_album("g")
    assert await queue_count_waiting(db) == 2
    claimed = await queue_claim_next(db)
    assert claimed is not None and claimed[1] == "attachment"  # album precedes text
    rows = await db.execute_fetchall("SELECT payload FROM queue ORDER BY id")
    payloads = [json.loads(row[0]) for row in rows]
    assert len(payloads[0]["attachments"]) == 2
    assert payloads[1]["text"] == "between photos"


async def test_album_straggler_after_more_than_one_second_stays_grouped(db, tmp_path):
    intake = Intake(db, FakeBot(), chat_id=7, db_path=tmp_path / STATE_DB_NAME)
    albums = AlbumBuffer(wait_s=2.0)
    first = message(photo=[{"file_id": "p1"}], media_group_id="slow", message_id=20)
    second = message(photo=[{"file_id": "p2"}], media_group_id="slow", message_id=21)
    await intake.accept_album_item(first)
    await albums.add(first)
    await asyncio.sleep(1.6)
    await intake.accept_album_item(second)
    await albums.add(second)
    assert await albums.flush_due() == []
    await asyncio.sleep(2.1)
    assert await albums.flush_due() == ["slow"]
    await intake.finish_album("slow")
    rows = await db.execute_fetchall("SELECT payload FROM queue")
    assert len(json.loads(rows[0][0])["attachments"]) == 2


async def test_failed_queue_insert_does_not_claim_message_id(db, tmp_path):
    await db.execute(
        "CREATE TRIGGER reject_queue BEFORE INSERT ON queue "
        "BEGIN SELECT RAISE(ABORT, 'queue unavailable'); END"
    )
    await db.commit()
    intake = Intake(db, FakeBot(), chat_id=7, db_path=tmp_path / STATE_DB_NAME)
    with pytest.raises(sqlite3.IntegrityError, match="queue unavailable"):
        await intake.accept(message(text="must not be lost", message_id=40))
    assert await kv_get(db, "last_message_id") is None
    assert await queue_count_waiting(db) == 0
