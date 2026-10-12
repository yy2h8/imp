"""Durable result delivery, deferred while interactive work is active."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Self

from .db import (
    delivery_claim,
    delivery_next_due,
    delivery_recover,
    delivery_retry,
    delivery_sent,
)
from .turns import error_detail

_LOG = logging.getLogger(__name__)
RETRY_SECONDS = 5
MAX_RETRY_SECONDS = 300


class Outbox:
    def __init__(
        self,
        db_path: Path,
        sender: Callable[[int, str], Awaitable[list[int]]],
        *,
        secrets: tuple[str, ...] = (),
    ) -> None:
        self.db_path = db_path
        self.sender = sender
        self.secrets = secrets
        self._turns_active = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._wake = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None
        self._needs_recovery = False

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[Self]:
        self._turns_active += 1
        self._idle.clear()
        try:
            yield self
        finally:
            self._turns_active -= 1
            if not self._turns_active:
                self._idle.set()
                self.wake()

    def wake(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        if self._worker is None:
            await delivery_recover(self.db_path)
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def deliver_now(self, turn_id: int) -> bool:
        async with self._send_lock:
            try:
                row = await self._claim(turn_id)
                return await self._send(row) if row else False
            except BaseException:
                self._needs_recovery = True
                self.wake()
                raise

    async def _claim(self, turn_id: int | None = None) -> dict | None:
        task = asyncio.create_task(
            delivery_claim(self.db_path, datetime.now(UTC).isoformat(), turn_id)
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            row = await task
            if row:
                await delivery_retry(
                    self.db_path,
                    row["id"],
                    error="Delivery interrupted before send",
                    next_attempt_at=datetime.now(UTC).isoformat(),
                )
            raise

    async def _send(self, row: dict) -> bool:
        try:
            ids = await self.sender(row["chat_id"], row["text"])
            if not ids or any(i is None for i in ids):
                raise ValueError("Required message delivery returned no receipt")
            await delivery_sent(self.db_path, row["id"], ids)
        except asyncio.CancelledError:
            await delivery_retry(
                self.db_path,
                row["id"],
                error="Delivery interrupted; send outcome unknown",
                next_attempt_at=datetime.now(UTC).isoformat(),
            )
            raise
        except Exception as exc:
            delay = min(
                MAX_RETRY_SECONDS, RETRY_SECONDS * 2 ** min(row["attempts"] - 1, 6)
            )
            delay = max(delay, getattr(exc, "retry_after", 0))
            await delivery_retry(
                self.db_path,
                row["id"],
                error=error_detail(exc, self.secrets),
                next_attempt_at=(
                    datetime.now(UTC) + timedelta(seconds=delay)
                ).isoformat(),
            )
            _LOG.warning("delivery %s deferred: %s", row["id"], type(exc).__name__)
            return False
        return True

    async def _run(self) -> None:
        while True:
            try:
                await self._idle.wait()
                self._wake.clear()
                async with self._send_lock:
                    if self._needs_recovery:
                        await delivery_recover(self.db_path)
                        self._needs_recovery = False
                    if self._turns_active:
                        continue
                    row = await self._claim()
                    if row:
                        if self._turns_active:
                            await delivery_retry(
                                self.db_path,
                                row["id"],
                                error=row["last_error"] or "",
                                next_attempt_at=datetime.now(UTC).isoformat(),
                            )
                        else:
                            await self._send(row)
                        continue
                due = await delivery_next_due(self.db_path)
                timeout = (
                    max(
                        0,
                        (
                            datetime.fromisoformat(due) - datetime.now(UTC)
                        ).total_seconds(),
                    )
                    if due
                    else None
                )
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout)
                except TimeoutError:
                    pass
            except Exception as exc:
                _LOG.warning("outbox storage unavailable: %s", type(exc).__name__)
                self._needs_recovery = True
                await asyncio.sleep(RETRY_SECONDS)
