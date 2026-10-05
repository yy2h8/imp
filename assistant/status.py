"""One resilient status summary for /status and the startup message.

Each section is gathered independently; a failing source degrades to
«недоступен» instead of hiding the rest. collect_status never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from .config import OPENROUTER_BASE_URL
from .db import jobs_meta_list, queue_count_waiting, transcript_last_reply

_LOG = logging.getLogger(__name__)

UNAVAILABLE = "недоступен"
NO_JOBS = "нет активных заданий"
_KIB = 1024 * 1024  # /proc/meminfo kB → GiB
BALANCE_TIMEOUT_S = 10.0
LABEL_LIMIT = 40
JOBS_SHOWN = 3
PREVIEW = 80


def format_tokens(used: int, maximum: int) -> str:
    return f"~{used:,} / {maximum:,} токенов (оценка)".replace(",", " ")


def host_summary(home: Path) -> str:
    """Linux stdlib probes: load average, /proc/meminfo, statvfs."""
    load1, _, _ = os.getloadavg()
    info: dict[str, int] = {}
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            name, _, value = line.partition(":")
            info[name] = int(value.strip().split()[0])  # kB
            if "MemTotal" in info and "MemAvailable" in info:
                break
    total = info["MemTotal"] / _KIB
    available = info["MemAvailable"] / _KIB
    ram_pct = (total - available) / total * 100
    disk = shutil.disk_usage(home)
    disk_free = disk.free / 2**30
    disk_pct = disk.used / disk.total * 100
    return (
        f"load {load1:.2f} · RAM {total - available:.1f}/{total:.1f} ГБ "
        f"({ram_pct:.0f}%) · диск: свободно {disk_free:.0f} ГБ (занято {disk_pct:.0f}%)"
    )


async def _jobs_lines(
    scheduler, db: aiosqlite.Connection | None, tz: ZoneInfo
) -> list[str]:
    if scheduler is None:
        return [UNAVAILABLE]
    pending = [
        j for j in scheduler.get_jobs() if getattr(j, "next_run_time", None) is not None
    ]
    if not pending:
        return [NO_JOBS]
    pending.sort(key=lambda j: j.next_run_time)
    labels: dict[str, str] = {}
    if db is not None:
        try:
            labels = {
                row["schedule_id"]: row["label"] for row in await jobs_meta_list(db)
            }
        except Exception:
            _LOG.warning("job labels unavailable", exc_info=True)
    lines = []
    for entry in pending[:JOBS_SHOWN]:
        label = " ".join(labels.get(entry.id, entry.id).split())
        if len(label) > LABEL_LIMIT:
            label = label[:LABEL_LIMIT] + "…"
        local = entry.next_run_time.astimezone(tz)
        lines.append(f"• {label} — {local:%d.%m %H:%M}")
    return lines


def _elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s} с"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} мин {s} с"
    h, m = divmod(m, 60)
    return f"{h} ч {m} мин"


def _clip(text: str, limit: int = PREVIEW) -> str:
    line = " ".join(text.split())
    return line[:limit] + ("…" if len(line) > limit else "")


async def _turn_lines(app, tz: ZoneInfo) -> list[str]:
    lines: list[str] = []
    current = getattr(app, "current_turn", None)
    if current is not None:
        lines.append(
            f"▶ Текущий ход — {_elapsed(time.monotonic() - current.started_monotonic)}"
        )
        lines.append(f"«{_clip(current.prompt)}»")
        lines.append(
            f"инструментов: {current.tools} · "
            f"сейчас: {current.activity or 'ожидание ответа модели'}"
        )
        if current.last_reply:
            stamp = current.last_reply_at.astimezone(tz) if current.last_reply_at else None
            when = f"{stamp:%H:%M} " if stamp else ""
            lines.append(f"ответ модели {when}«{_clip(current.last_reply)}»")
    ask_router = getattr(app, "ask_router", None)
    if getattr(ask_router, "pending", False):
        lines.append("⏸ Ход ждёт вашего ответа на вопрос (ответьте обычным сообщением)")
    if app.db is not None:
        try:
            for row in await jobs_meta_list(app.db):
                if row.get("state") != "running":
                    continue
                label = " ".join((row.get("label") or row["schedule_id"]).split())
                if len(label) > LABEL_LIMIT:
                    label = label[:LABEL_LIMIT] + "…"
                updated = datetime.fromisoformat(row["updated_at"])
                lines.append(
                    f"⏳ Фоновое задание: {label} — идёт "
                    f"{_elapsed((datetime.now(UTC) - updated).total_seconds())}"
                )
        except Exception:
            _LOG.warning("running-jobs section failed", exc_info=True)
    if current is None and getattr(app, "session", None) is not None:
        try:
            last = await transcript_last_reply(app.db, app.session.writer.session_id)
        except Exception:
            last = None
        if last is not None:
            ts, text = last
            local = datetime.fromisoformat(ts).astimezone(tz)
            lines.append(f"💬 Последний ответ {local:%d.%m %H:%M}: «{_clip(text)}»")
    if app.db is not None:
        try:
            waiting = await queue_count_waiting(app.db)
        except Exception:
            waiting = 0
        if waiting:
            lines.append(f"📥 В очереди: {waiting}")
    return lines


async def collect_status(app, now: datetime | None = None) -> str:
    try:
        return await _collect(app, now)
    except Exception:
        _LOG.exception("status collection failed")
        return f"*Статус*\n\n{UNAVAILABLE}"


async def _collect(app, now: datetime | None) -> str:
    tz_name = app.assistant.tz
    tz = ZoneInfo(tz_name)
    moment = now or datetime.now(tz)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=tz)
    moment = moment.astimezone(tz)

    lines = [
        "*Статус*",
        "",
        f"🕐 {moment:%d.%m.%Y %H:%M} ({tz_name})",
        "",
        "⏰ Задания:",
    ]
    try:
        lines.extend(await _jobs_lines(app.scheduler, app.db, tz))
    except Exception:
        _LOG.warning("jobs section failed", exc_info=True)
        lines.append(UNAVAILABLE)
    lines.append("")
    try:
        lines.extend(await _turn_lines(app, tz))
    except Exception:
        _LOG.warning("turn section failed", exc_info=True)
        lines.append(UNAVAILABLE)
    lines.append("")
    try:
        used, maximum = app.usage
        context = format_tokens(used, maximum)
    except Exception:
        _LOG.warning("context section failed", exc_info=True)
        context = UNAVAILABLE
    lines.append(f"🧠 Контекст: {context}")
    lines.append("")
    try:
        host = await asyncio.to_thread(host_summary, Path(app.assistant.home))
    except Exception:
        _LOG.warning("host section failed", exc_info=True)
        host = UNAVAILABLE
    lines.append(f"🖥 Хост: {host}")
    lines.append("")
    http = getattr(app, "http", None)
    try:
        balance = UNAVAILABLE if http is None else await _balance_line(http, app)
    except Exception:
        _LOG.warning("balance section failed", exc_info=True)
        balance = UNAVAILABLE
    lines.append(f"💳 OpenRouter: {balance}")
    return "\n".join(lines)


async def openrouter_balance(http, api_key: str, timeout: float = BALANCE_TIMEOUT_S) -> float | None:
    """Wallet balance in USD: /credits (management key), falling back to the
    inference key's remaining limit. None when undeterminable."""
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        raw = await asyncio.wait_for(
            http.get(f"{OPENROUTER_BASE_URL}/credits", headers=headers), timeout
        )
        data = json.loads(raw)["data"]
        return float(data["total_credits"]) - float(data["total_usage"])
    except Exception as exc:
        _LOG.warning("openrouter credits unavailable: %s", type(exc).__name__)
    try:
        raw = await asyncio.wait_for(
            http.get(f"{OPENROUTER_BASE_URL}/key", headers=headers), timeout
        )
        remaining = json.loads(raw)["data"]["limit_remaining"]
        return None if remaining is None else float(remaining)
    except Exception as exc:
        _LOG.warning("openrouter key fallback unavailable: %s", type(exc).__name__)
        return None


async def _balance_line(http, app) -> str:
    balance = await openrouter_balance(http, app.config.api_key)
    return UNAVAILABLE if balance is None else f"${balance:.2f}"
