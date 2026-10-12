"""Shared usage checkpoints for interactive turns and scheduled runs."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from imp.events import AgentEvent, EventType

from .db import ERROR_DETAIL_CHARS, turn_checkpoint, turn_finish, turn_start


def error_detail(error: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(error)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text[:ERROR_DETAIL_CHARS]


@dataclass(slots=True)
class Run:
    path: Path
    id: int
    session_id: str
    secrets: tuple[str, ...] = ()
    started: float = field(default_factory=time.monotonic)
    in_tokens: int = 0
    out_tokens: int = 0
    cost: float | None = None
    tools: int = 0
    missing_usage: bool = False
    in_flight: bool = False
    error: str | None = None
    error_code: str | None = None
    finished: bool = False
    cancelled_during_start: bool = False

    @classmethod
    async def start(
        cls,
        path: Path,
        *,
        kind: str,
        model: str,
        session_id: str,
        queue_id: int | None = None,
        schedule_id: str | None = None,
        secrets: tuple[str, ...] = (),
    ) -> Run:
        started = time.monotonic()
        creation = asyncio.create_task(
            turn_start(
                path,
                kind=kind,
                model=model,
                session_id=session_id,
                queue_id=queue_id,
                schedule_id=schedule_id,
            )
        )
        cancelled = False
        try:
            run_id = await asyncio.shield(creation)
        except asyncio.CancelledError:
            run_id = await creation
            cancelled = True
        return cls(
            path,
            run_id,
            session_id,
            secrets,
            started=started,
            cancelled_during_start=cancelled,
        )

    @property
    def cost_complete(self) -> bool:
        return not self.missing_usage and not self.in_flight

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started

    async def observe(self, event: AgentEvent) -> None:
        if event.type is EventType.MODEL_RESPONSE and event.usage is not None:
            return  # already checkpointed by MODEL_USAGE before transcript/display work
        if event.type is EventType.THINKING:
            self.in_flight = True
        elif event.type in (
            EventType.MODEL_USAGE,
            EventType.MODEL_RESPONSE,
            EventType.ERROR,
        ):
            if (
                event.type in (EventType.MODEL_USAGE, EventType.MODEL_RESPONSE)
                or event.usage is not None
                or event.error_code in ("model_error", "invalid_response")
            ):
                self.in_flight = False
                if event.usage is None:
                    self.missing_usage = True
                else:
                    self.in_tokens += event.usage.input_tokens
                    self.out_tokens += event.usage.output_tokens
                    if event.usage.cost_usd is None:
                        self.missing_usage = True
                    else:
                        self.cost = (self.cost or 0.0) + event.usage.cost_usd
            if event.type is EventType.ERROR:
                self.error = error_detail(
                    event.error_message or "Turn failed", self.secrets
                )
                self.error_code = event.error_code or "runtime_error"
        elif event.type is EventType.TOOL_START:
            self.tools += 1
        else:
            return
        await self.checkpoint()

    async def checkpoint(self) -> None:
        await turn_checkpoint(
            self.path,
            self.id,
            session_id=self.session_id,
            in_tokens=self.in_tokens,
            out_tokens=self.out_tokens,
            cost_usd=self.cost,
            cost_complete=self.cost_complete,
            tools=self.tools,
            seconds=self.seconds,
        )

    async def finish(
        self,
        status: str,
        chat_id: int,
        text: str,
        *,
        code: str | None = None,
        error: object | None = None,
        job_state: str | None = None,
        job_result: str | None = None,
    ) -> None:
        if self.finished:
            return
        await self.checkpoint()
        await turn_finish(
            self.path,
            self.id,
            status=status,
            error_code=code or self.error_code,
            error_message=error_detail(error, self.secrets)
            if error is not None
            else self.error,
            chat_id=chat_id,
            text=text,
            job_state=job_state,
            job_result=job_result,
        )
        self.finished = True
