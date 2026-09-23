"""imp-assistant: a Telegram personal assistant built on imp as a library.

One owner talks to the bot over Telegram; imp's agent core provides the
capability set, this package provides the transport (adapters/telegram.py),
rendering (adapters/ui.py), STT (adapters/stt.py), the composition root
(app.py), bootstrap (bootstrap.py), the job store (jobstore.py) and scheduler
(scheduler.py). Run with `python -m assistant` (`whoami` to find your id).
See README.md.
"""

__all__: list[str] = []
