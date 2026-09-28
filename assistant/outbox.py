"""Outbox: one ordered delivery lane for job results.

Job results wait while any interactive turn is active (turn_scope) so they
never interleave with a turn's live status edits and final answer; turn
output itself goes direct. Delivery failures are logged and the item is
dropped — job results are also recorded in jobs_meta.delivery by the
scheduler, so the outbox never blocks or retries.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncContextManager, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Self

_LOG = logging.getLogger(__name__)


class Outbox:
    def __init__(self, sender: Callable[[str], Awaitable[object]]) -> None:
        self.sender = sender
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._turns_active = 0
        self._idle = asyncio.Event()
        self._idle.set()  # no turn active: deliveries flow
        self._worker: asyncio.Task | None = None

    @asynccontextmanager
    async def turn_scope(self) -> AsyncContextManager[Self]:
        """Mark an interactive turn active; deferred items resume on exit."""
        self._turns_active += 1
        self._idle.clear()
        try:
            yield self
        finally:
            self._turns_active -= 1
            if self._turns_active == 0:
                self._idle.set()

    async def submit(self, text: str) -> None:
        self._queue.put_nowait(text)

    async def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Drain pending items, then end the worker."""
        await self._queue.join()
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None

    async def _run(self) -> None:
        while True:
            text = await self._queue.get()
            try:
                await self._idle.wait()  # hold job results between turns
                await self.sender(text)
            except Exception as exc:  # cosmetic: drop and keep the lane alive
                _LOG.warning("outbox delivery failed: %s", exc)
            finally:
                self._queue.task_done()
