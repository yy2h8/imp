"""STT over the OpenRouter audio-transcriptions endpoint via the OpenAI SDK.

OpenRouter accepts OpenAI-compatible multipart on /audio/transcriptions, so the
same AsyncOpenAI client the agent already uses (base_url pinned to OpenRouter)
transcribes with no extra dependency. One method, tool-result semantics.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from openai import AsyncOpenAI


class SttClient:
    """Speech-to-text: file in, transcript text out, error text on failure."""

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model

    async def transcribe(self, path: Path) -> str:
        """Transcribe an audio file; returns transcript or 'STT failed: …'."""
        try:
            data = await asyncio.to_thread(path.read_bytes)
            result = await self.client.audio.transcriptions.create(
                model=self.model,
                file=(path.name, data),
            )
            text = (result.text or "").strip()
            return text if text else "STT failed: empty transcript"
        except Exception as exc:
            return f"STT failed: {exc}"
