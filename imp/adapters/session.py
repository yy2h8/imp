from __future__ import annotations

import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, TextIO

from ..entities import ConversationMessage

# lazy: single-session by design; multi-session/switching = open another writer
# and reassign Context.writer, resume = parse lines back via serialize()'s inverse


class SessionWriter:
    """Appends each conversation message to a jsonl file, one line per message.
    Persistence only — the agent always runs from the in-memory Context."""

    def __init__(self, workspace: Path) -> None:
        sessions_dir = workspace / ".imp" / "sessions"
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self.path = sessions_dir / f"{stamp}-{secrets.token_hex(2)}.jsonl"
        self._fh: TextIO | None = None

    def __enter__(self) -> Self:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            self._disable(exc)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fh is not None:
            self._fh.close()

    def write(self, message: ConversationMessage) -> None:
        # lazy: sync append+flush on the event loop; writes are tiny — to_thread if sessions ever lag
        if self._fh is None:
            return
        try:
            self._fh.write(
                json.dumps(message.serialize(), ensure_ascii=False, default=str) + "\n"
            )
            self._fh.flush()
        except OSError as exc:
            self._disable(exc)

    def _disable(self, exc: OSError) -> None:
        fh, self._fh = self._fh, None
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
        print(f"warning: session persistence disabled: {exc}", file=sys.stderr)
