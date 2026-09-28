"""Outbox: ordered delivery lane that defers job results while a turn runs."""

from __future__ import annotations

import asyncio

from assistant.outbox import Outbox


class FakeSender:
    def __init__(self, failures: set[int] | None = None) -> None:
        self.sent: list[str] = []
        self.calls = 0
        self.failures = failures or set()

    async def __call__(self, text: str) -> None:
        self.calls += 1
        if self.calls in self.failures:
            raise RuntimeError("send failed")
        self.sent.append(text)


async def test_delivers_in_order_without_turn():
    sender = FakeSender()
    box = Outbox(sender)
    await box.start()
    await box.submit("first")
    await box.submit("second")
    await box.stop()
    assert sender.sent == ["first", "second"]


async def test_defers_while_turn_active():
    sender = FakeSender()
    box = Outbox(sender)
    await box.start()
    async with box.turn_scope():
        await box.submit("held")
        await asyncio.sleep(0.05)
        assert sender.sent == []  # deferred: a turn is active
    for _ in range(50):
        await asyncio.sleep(0.02)
        if sender.sent:
            break
    assert sender.sent == ["held"]  # delivered right after the scope exits
    await box.stop()


async def test_failed_send_drops_item_and_continues():
    sender = FakeSender(failures={1})  # first send raises
    box = Outbox(sender)
    await box.start()
    await box.submit("doomed")
    await box.submit("after")
    await box.stop()
    assert sender.sent == ["after"]  # failed item dropped, later items flow


async def test_stop_drains_pending():
    sender = FakeSender()
    box = Outbox(sender)
    await box.start()
    await box.submit("one")
    await box.submit("two")
    await box.stop()
    assert sender.sent == ["one", "two"]
