"""imp-assistant: a Telegram personal assistant built on imp as a library.

One owner talks to the bot over Telegram; imp's agent core provides the
capability set, this package provides the transport (adapters/telegram.py),
rendering (adapters/ui.py), STT (adapters/stt.py), the composition root
(app.py), bootstrap (bootstrap.py), SQLite state (`db.py`), transcripts,
intake and APScheduler wiring. Run with `python -m assistant` (`whoami` to find
your id). The Telegram framework is aiogram.
See README.md.
"""

__all__: list[str] = []
