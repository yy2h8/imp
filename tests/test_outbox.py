from __future__ import annotations

import asyncio

import pytest

from assistant.db import STATE_DB_NAME, open_db, turn_finish, turn_start
from assistant.outbox import Outbox


@pytest.fixture
async def store(tmp_path):
    path = tmp_path / STATE_DB_NAME
    db = await open_db(path)
    yield path, db
    await db.close()


async def enqueue(path, text):
    run = await turn_start(path, kind="job", model="m", session_id="s")
    await turn_finish(
        path,
        run,
        status="completed",
        error_code=None,
        error_message=None,
        chat_id=42,
        text=text,
    )
    return run


async def test_failed_send_keeps_result_and_later_results_flow(store):
    path, db = store
    sent = []

    async def sender(chat, text):
        assert chat == 42
        if text == "first":
            raise RuntimeError("offline")
        sent.append(text)
        return [10]

    box = Outbox(path, sender)
    first = await enqueue(path, "first")
    second = await enqueue(path, "second")
    assert not await box.deliver_now(first)
    assert await box.deliver_now(second)
    assert sent == ["second"]
    assert await db.execute_fetchall(
        "SELECT state,attempts FROM deliveries ORDER BY id"
    ) == [("pending", 1), ("sent", 1)]


async def test_defers_background_and_immediate_send_claims_once(store):
    path, _db = store
    sent = asyncio.Event()
    texts = []

    async def sender(chat, text):
        texts.append(text)
        sent.set()
        return [11]

    box = Outbox(path, sender)
    await box.start()
    try:
        async with box.turn_scope():
            run = await enqueue(path, "answer")
            box.wake()
            await asyncio.sleep(0.02)
            assert texts == []
            assert await box.deliver_now(run)
            assert not await box.deliver_now(run)
        await asyncio.wait_for(sent.wait(), 1)
        assert texts == ["answer"]
    finally:
        await box.stop()


async def test_shutdown_keeps_ambiguous_send_for_restart(store):
    path, db = store
    entered = asyncio.Event()

    async def blocked(chat, text):
        entered.set()
        await asyncio.Event().wait()

    await enqueue(path, "original")
    box = Outbox(path, blocked)
    await box.start()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(box.stop(), 1)
    assert await db.execute_fetchall("SELECT state FROM deliveries") == [("pending",)]
    received = asyncio.Event()
    texts = []

    async def sender(chat, text):
        texts.append((chat, text))
        received.set()
        return [12]

    box = Outbox(path, sender)
    await box.start()
    try:
        await asyncio.wait_for(received.wait(), 1)
        for _ in range(50):
            if (await db.execute_fetchall("SELECT state FROM deliveries")) == [
                ("sent",)
            ]:
                break
            await asyncio.sleep(0.01)
        assert texts == [(42, "original")]
        assert await db.execute_fetchall(
            "SELECT state,message_ids FROM deliveries"
        ) == [("sent", "[12]")]
    finally:
        await box.stop()


async def test_retry_after_and_redaction_are_persisted(store):
    from datetime import UTC, datetime

    from assistant.adapters.telegram import TelegramError

    path, db = store
    secret = "test-provider-secret"

    async def reject(chat, text):
        raise TelegramError("URL contains " + secret, retry_after=600)

    run = await enqueue(path, "answer")
    before = datetime.now(UTC)
    box = Outbox(path, reject, secrets=(secret,))
    assert not await box.deliver_now(run)
    error, due = (
        await db.execute_fetchall("SELECT last_error,next_attempt_at FROM deliveries")
    )[0]
    assert secret not in error and "<redacted>" in error
    assert (datetime.fromisoformat(due) - before).total_seconds() >= 600


async def test_retry_sends_original_text_after_partial_send(store):
    path, db = store
    parts = []
    calls = 0

    async def partial(chat, text):
        nonlocal calls
        calls += 1
        parts.append(text[:4])
        if calls == 1:
            raise RuntimeError("failed after first part")
        parts.append(text[4:])
        return [1, 2]

    run = await enqueue(path, "long original")
    box = Outbox(path, partial)
    assert not await box.deliver_now(run)
    await db.execute("UPDATE deliveries SET next_attempt_at='2000-01-01'")
    await db.commit()
    assert await box.deliver_now(run)
    assert parts == ["long", "long", " original"]
    assert await db.execute_fetchall("SELECT state FROM deliveries") == [("sent",)]


async def test_cancelled_immediate_send_is_retryable_without_restart(store):
    path, db = store
    entered = asyncio.Event()

    async def blocked(chat, text):
        entered.set()
        await asyncio.Event().wait()

    run = await enqueue(path, "answer")
    box = Outbox(path, blocked)
    task = asyncio.create_task(box.deliver_now(run))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.execute_fetchall("SELECT state FROM deliveries") == [("pending",)]


async def test_cancel_during_claim_returns_row_to_pending(store, monkeypatch):
    import assistant.outbox as module

    path, db = store
    claimed, release = asyncio.Event(), asyncio.Event()
    real_claim = module.delivery_claim

    async def delayed(*args, **kwargs):
        row = await real_claim(*args, **kwargs)
        claimed.set()
        await release.wait()
        return row

    monkeypatch.setattr(module, "delivery_claim", delayed)

    async def sender(chat, text):
        raise AssertionError("cancelled claim must not send")

    run = await enqueue(path, "answer")
    box = Outbox(path, sender)
    task = asyncio.create_task(box.deliver_now(run))
    await asyncio.wait_for(claimed.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.execute_fetchall("SELECT state,attempts FROM deliveries") == [
        ("pending", 1)
    ]


async def test_cancel_after_receipt_does_not_revoke_sent_state(store, monkeypatch):
    import assistant.outbox as module

    path, db = store
    recorded, release = asyncio.Event(), asyncio.Event()
    real_sent = module.delivery_sent

    async def delayed(*args, **kwargs):
        await real_sent(*args, **kwargs)
        recorded.set()
        await release.wait()

    monkeypatch.setattr(module, "delivery_sent", delayed)

    async def sender(chat, text):
        return [11]

    run = await enqueue(path, "answer")
    box = Outbox(path, sender)
    task = asyncio.create_task(box.deliver_now(run))
    await asyncio.wait_for(recorded.wait(), 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await db.execute_fetchall("SELECT state,message_ids FROM deliveries") == [
        ("sent", "[11]")
    ]


async def test_retry_storage_failure_recovers_without_restart(store, monkeypatch):
    import assistant.outbox as module

    path, db = store
    real_retry = module.delivery_retry
    writes = 0

    async def fail_once(*args, **kwargs):
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("temporary storage failure")
        return await real_retry(*args, **kwargs)

    monkeypatch.setattr(module, "delivery_retry", fail_once)
    calls = 0
    sent = asyncio.Event()

    async def sender(chat, text):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("network unavailable")
        sent.set()
        return [12]

    box = Outbox(path, sender)
    await box.start()
    try:
        async with box.turn_scope():
            run = await enqueue(path, "answer")
            with pytest.raises(OSError):
                await box.deliver_now(run)
        await asyncio.wait_for(sent.wait(), 0.5)
        assert calls == 2
        for _ in range(50):
            if await db.execute_fetchall("SELECT state FROM deliveries") == [("sent",)]:
                break
            await asyncio.sleep(0.01)
        assert await db.execute_fetchall("SELECT state FROM deliveries") == [("sent",)]
    finally:
        await box.stop()
