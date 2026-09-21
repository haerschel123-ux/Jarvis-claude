"""Reminders and scheduled tasks (Spec §27, §28, §97).

Design points that matter:

* **Firing needs no model.** A reminder is stored with its next run time and fires from a
  local loop, so it works offline and cannot be delayed by a provider outage (Spec §97).
* **Missed reminders are detected.** After a restart, anything whose time has passed fires
  once and is counted as missed — a reminder is not silently lost because the PC was off.
* **Recurring reminders stay on local wall-clock time.** The next occurrence is computed in
  the reminder's own timezone, so a daily 18:00 reminder stays at 18:00 across a DST change.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.enums import ScheduleKind
from core.events import EventType, event_bus
from core.logging_setup import get_logger
from memory.database import Database, db

log = get_logger("scheduler")

TICK_SECONDS = 20.0
# A reminder whose time passed longer ago than this is reported as missed rather than fired
# as if it were due now.
MISSED_GRACE = timedelta(minutes=5)

WEEKDAY_NAMES = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4,
    "samstag": 5, "sonnabend": 5, "sonntag": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
}


def _zone(name: str) -> ZoneInfo | UTC.__class__:  # type: ignore[valid-type]
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def _to_utc(moment: datetime, timezone: str) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone(timezone))
    return moment.astimezone(UTC)


def _parse_iso(value: str) -> datetime:
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


@dataclass(slots=True)
class ParsedSchedule:
    """The result of interpreting a spoken or typed reminder phrase."""

    when: datetime                       # UTC
    kind: ScheduleKind = ScheduleKind.ONE_TIME
    interval_seconds: int | None = None
    weekdays: list[int] | None = None
    day_of_month: int | None = None
    text: str = ""
    timezone: str = "UTC"

    def to_dict(self) -> dict[str, Any]:
        return {
            "when": self.when.isoformat(),
            "kind": self.kind.value,
            "interval_seconds": self.interval_seconds,
            "weekdays": self.weekdays,
            "day_of_month": self.day_of_month,
            "text": self.text,
            "timezone": self.timezone,
        }


_TIME = re.compile(r"(?i)\b(?:um\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:uhr|h)?\b")
_IN_RELATIVE = re.compile(
    r"(?i)\bin\s+(\d{1,3})\s*(minute|minuten|min|stunde|stunden|std|tag|tagen|woche|wochen)\b")
_EVERY = re.compile(
    r"(?i)\b(jeden|jede|alle)\s+(\d{1,3})?\s*"
    r"(tag|tage|woche|wochen|monat|monate|stunde|stunden|minute|minuten|"
    r"montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonnabend|sonntag)\b")
_TOMORROW = re.compile(r"(?i)\bmorgen\b")
_DAY_AFTER = re.compile(r"(?i)\bübermorgen\b")
_TODAY = re.compile(r"(?i)\bheute\b")


def parse_schedule(text: str, *, timezone: str = "UTC", now: datetime | None = None) -> ParsedSchedule | None:
    """Interpret a German reminder phrase. Returns None when no time is recognisable.

    Deliberately rule-based: a reminder must be creatable without a model, offline, and
    without the latency of a round trip (Spec §97).
    """
    if not text or not text.strip():
        return None
    zone = _zone(timezone)
    local_now = (now or datetime.now(UTC)).astimezone(zone)
    body = _strip_lead_in(text)

    if match := _IN_RELATIVE.search(text):
        amount = int(match.group(1))
        unit = match.group(2).lower()
        delta = {
            "minute": timedelta(minutes=amount), "minuten": timedelta(minutes=amount),
            "min": timedelta(minutes=amount),
            "stunde": timedelta(hours=amount), "stunden": timedelta(hours=amount),
            "std": timedelta(hours=amount),
            "tag": timedelta(days=amount), "tagen": timedelta(days=amount),
            "woche": timedelta(weeks=amount), "wochen": timedelta(weeks=amount),
        }[unit]
        return ParsedSchedule(
            when=(local_now + delta).astimezone(UTC), text=body, timezone=timezone,
        )

    if match := _EVERY.search(text):
        count = int(match.group(2) or 1)
        unit = match.group(3).lower()
        clock = _extract_time(text)

        if unit in WEEKDAY_NAMES:
            weekday = WEEKDAY_NAMES[unit]
            target = _next_weekday(local_now, weekday, clock)
            return ParsedSchedule(
                when=target.astimezone(UTC), kind=ScheduleKind.WEEKLY,
                weekdays=[weekday], text=body, timezone=timezone,
            )
        if unit.startswith("tag"):
            target = _apply_time(local_now, clock or time(9, 0))
            if target <= local_now:
                target += timedelta(days=count)
            return ParsedSchedule(when=target.astimezone(UTC), kind=ScheduleKind.DAILY,
                                  interval_seconds=count * 86400, text=body, timezone=timezone)
        if unit.startswith("woche"):
            target = _apply_time(local_now, clock or time(9, 0)) + timedelta(weeks=count)
            return ParsedSchedule(when=target.astimezone(UTC), kind=ScheduleKind.WEEKLY,
                                  interval_seconds=count * 604800, text=body, timezone=timezone)
        if unit.startswith("monat"):
            target = _add_months(_apply_time(local_now, clock or time(9, 0)), count)
            return ParsedSchedule(when=target.astimezone(UTC), kind=ScheduleKind.MONTHLY,
                                  day_of_month=local_now.day, text=body, timezone=timezone)
        seconds = count * (3600 if unit.startswith("stunde") else 60)
        return ParsedSchedule(
            when=(local_now + timedelta(seconds=seconds)).astimezone(UTC),
            kind=ScheduleKind.INTERVAL, interval_seconds=seconds, text=body, timezone=timezone,
        )

    clock = _extract_time(text)
    base = local_now
    if _DAY_AFTER.search(text):
        base = local_now + timedelta(days=2)
    elif _TOMORROW.search(text):
        base = local_now + timedelta(days=1)
    elif _TODAY.search(text):
        base = local_now

    if clock is not None:
        target = _apply_time(base, clock)
        if target <= local_now:
            target += timedelta(days=1)
        return ParsedSchedule(when=target.astimezone(UTC), text=body, timezone=timezone)

    if _TOMORROW.search(text) or _DAY_AFTER.search(text):
        # A day without a time defaults to a reasonable morning hour.
        return ParsedSchedule(when=_apply_time(base, time(9, 0)).astimezone(UTC),
                              text=body, timezone=timezone)
    return None


def _strip_lead_in(text: str) -> str:
    """Remove the request wrapper so the stored reminder reads as the thing to do."""
    body = re.sub(
        r"(?i)^\s*(?:jarvis[,\s]+)?(?:bitte\s+)?erinnere?\s+mich\s+"
        r"(?:daran[,\s]+|dran[,\s]+)?", "", text)
    body = re.sub(r"(?i)^\s*(?:jarvis[,\s]+)?(?:bitte\s+)?(?:erinnerung|remind\s+me)\b[:,]?\s*",
                  "", body)
    body = _IN_RELATIVE.sub("", body)
    body = _EVERY.sub("", body)
    body = re.sub(r"(?i)\b(?:um\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:uhr|h)\b", "", body)
    body = re.sub(r"(?i)\b(heute|morgen|übermorgen)\b", "", body)
    body = re.sub(r"(?i)^\s*(?:daran[,\s]+|dran[,\s]+|dass\s+|zu\s+|an\s+)", "", body)
    body = re.sub(r"\s{2,}", " ", body).strip(" ,.;:!?-")
    return body or text.strip()


def _extract_time(text: str) -> time | None:
    for match in _TIME.finditer(text):
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            # A bare small number without "Uhr" is more likely a quantity than a clock time.
            if match.group(2) is None and not re.search(r"(?i)uhr|h\b", match.group(0)):
                continue
            return time(hour, minute)
    return None


def _apply_time(moment: datetime, clock: time) -> datetime:
    return moment.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)


def _next_weekday(now: datetime, weekday: int, clock: time | None) -> datetime:
    target = _apply_time(now, clock or time(9, 0))
    days_ahead = (weekday - now.weekday()) % 7
    if days_ahead == 0 and target <= now:
        days_ahead = 7
    return target + timedelta(days=days_ahead)


def _add_months(moment: datetime, months: int) -> datetime:
    month = moment.month - 1 + months
    year = moment.year + month // 12
    month = month % 12 + 1
    # Clamp to the last valid day, so 31 January plus one month lands on 28/29 February.
    day = min(moment.day, [31, 29 if _leap(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return moment.replace(year=year, month=month, day=day)


def _leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def next_occurrence(reminder: dict[str, Any], after: datetime | None = None) -> datetime | None:
    """Compute the next run time for a recurring reminder, in UTC."""
    kind = str(reminder.get("schedule_kind") or ScheduleKind.ONE_TIME.value)
    if kind == ScheduleKind.ONE_TIME.value:
        return None

    timezone = str(reminder.get("timezone") or "UTC")
    zone = _zone(timezone)
    reference = (after or datetime.now(UTC)).astimezone(zone)
    previous = _parse_iso(str(reminder["next_run_at"])).astimezone(zone)
    clock = time(previous.hour, previous.minute)

    if kind == ScheduleKind.DAILY.value:
        step = max(int(reminder.get("interval_seconds") or 86400) // 86400, 1)
        target = _apply_time(reference, clock)
        while target <= reference:
            target += timedelta(days=step)
        return target.astimezone(UTC)

    if kind == ScheduleKind.WEEKLY.value:
        raw = reminder.get("weekdays")
        weekdays = json.loads(raw) if isinstance(raw, str) and raw else (raw or [previous.weekday()])
        candidates = [_next_weekday(reference, int(day), clock) for day in weekdays]
        return min(candidates).astimezone(UTC) if candidates else None

    if kind == ScheduleKind.MONTHLY.value:
        target = _add_months(_apply_time(reference, clock), 1)
        day = int(reminder.get("day_of_month") or previous.day)
        target = target.replace(day=min(day, 28)) if day > 28 else target.replace(day=day)
        while target <= reference:
            target = _add_months(target, 1)
        return target.astimezone(UTC)

    if kind == ScheduleKind.INTERVAL.value:
        seconds = int(reminder.get("interval_seconds") or 3600)
        target = previous
        while target <= reference:
            target += timedelta(seconds=seconds)
        return target.astimezone(UTC)

    return None


class Scheduler:
    """Background loop that fires reminders and scheduled automations."""

    def __init__(self, database: Database | None = None) -> None:
        self._db = database or db
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    # --- reminders ----------------------------------------------------------------------

    async def create_reminder(
        self,
        text: str,
        when: datetime,
        *,
        kind: ScheduleKind = ScheduleKind.ONE_TIME,
        timezone: str = "UTC",
        interval_seconds: int | None = None,
        weekdays: list[int] | None = None,
        day_of_month: int | None = None,
        project_id: int | None = None,
    ) -> int:
        reminder_id = await self._db.execute(
            """
            INSERT INTO reminders
                (text, schedule_kind, next_run_at, timezone, interval_seconds, weekdays,
                 day_of_month, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                text, kind.value, _to_utc(when, timezone).isoformat(), timezone,
                interval_seconds, json.dumps(weekdays) if weekdays else None,
                day_of_month, project_id,
            ),
        )
        log.info("Erinnerung #%d angelegt: %s (%s)", reminder_id, text, when.isoformat())
        return reminder_id

    async def create_from_text(
        self, text: str, *, timezone: str = "UTC", project_id: int | None = None
    ) -> tuple[int, ParsedSchedule] | None:
        parsed = parse_schedule(text, timezone=timezone)
        if parsed is None:
            return None
        reminder_id = await self.create_reminder(
            parsed.text, parsed.when, kind=parsed.kind, timezone=timezone,
            interval_seconds=parsed.interval_seconds, weekdays=parsed.weekdays,
            day_of_month=parsed.day_of_month, project_id=project_id,
        )
        return reminder_id, parsed

    async def due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        moment = (now or datetime.now(UTC)).isoformat()
        return await self._db.fetch_all(
            "SELECT * FROM reminders WHERE enabled = 1 AND next_run_at <= ? ORDER BY next_run_at",
            (moment,),
        )

    async def fire(self, reminder: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
        """Fire one reminder and schedule its next occurrence."""
        moment = now or datetime.now(UTC)
        scheduled = _parse_iso(str(reminder["next_run_at"]))
        missed = (moment - scheduled) > MISSED_GRACE

        upcoming = next_occurrence(reminder, moment)
        if upcoming is not None:
            await self._db.execute(
                "UPDATE reminders SET next_run_at = ?, last_fired_at = ?, "
                "fire_count = fire_count + 1, missed_count = missed_count + ? WHERE id = ?",
                (upcoming.isoformat(), moment.isoformat(), int(missed), reminder["id"]),
            )
        else:
            await self._db.execute(
                "UPDATE reminders SET enabled = 0, last_fired_at = ?, "
                "fire_count = fire_count + 1, missed_count = missed_count + ? WHERE id = ?",
                (moment.isoformat(), int(missed), reminder["id"]),
            )

        event_bus.emit(
            EventType.REMINDER_TRIGGERED,
            id=reminder["id"], text=reminder["text"],
            scheduled_for=scheduled.isoformat(), missed=missed,
            next_run_at=upcoming.isoformat() if upcoming else None,
        )
        log.info("Erinnerung ausgelöst: %s%s", reminder["text"], " (verpasst)" if missed else "")
        return {"id": reminder["id"], "text": reminder["text"], "missed": missed,
                "next_run_at": upcoming.isoformat() if upcoming else None}

    async def list_reminders(self, include_disabled: bool = False) -> list[dict[str, Any]]:
        clause = "" if include_disabled else "WHERE enabled = 1"
        return await self._db.fetch_all(
            f"SELECT * FROM reminders {clause} ORDER BY enabled DESC, next_run_at"
        )

    async def delete_reminder(self, reminder_id: int) -> bool:
        existing = await self._db.fetch_one("SELECT id FROM reminders WHERE id = ?", (reminder_id,))
        if existing is None:
            return False
        await self._db.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        return True

    async def set_reminder_enabled(self, reminder_id: int, enabled: bool) -> None:
        await self._db.execute("UPDATE reminders SET enabled = ? WHERE id = ?",
                               (int(enabled), reminder_id))

    # --- loop -----------------------------------------------------------------------------

    async def tick(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """One scheduler pass. Exposed separately so tests do not need to wait."""
        fired: list[dict[str, Any]] = []
        for reminder in await self.due(now):
            try:
                fired.append(await self.fire(reminder, now))
            except Exception:
                log.exception("Erinnerung #%s konnte nicht ausgelöst werden", reminder.get("id"))
        return fired

    async def _loop(self) -> None:
        log.info("Scheduler gestartet")
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("Scheduler-Durchlauf fehlgeschlagen")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                continue
        log.info("Scheduler beendet")

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        # Fire anything that came due while JARVIS was not running (Spec §27).
        missed = await self.tick()
        if missed:
            log.info("%d verpasste Erinnerung(en) beim Start ausgelöst", len(missed))
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None


scheduler = Scheduler()
