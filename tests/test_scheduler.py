"""Reminders, task graph and proactivity (Spec §14, §27, §28, §97, §102, §103)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.config import Settings
from core.enums import ProactivityMode, ScheduleKind, TaskStatus
from core.proactive import Importance, Notification, ProactiveEngine
from core.scheduler import Scheduler, next_occurrence, parse_schedule
from core.tasks import TaskManager
from memory.database import Database

TZ = "Europe/Berlin"
NOW = datetime(2026, 9, 21, 14, 30, tzinfo=ZoneInfo(TZ))   # a Monday


@pytest.fixture
def scheduler(database: Database) -> Scheduler:
    return Scheduler(database)


@pytest.fixture
def task_manager(database: Database) -> TaskManager:
    return TaskManager(database)


# --- phrase parsing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("phrase", "expected_local", "kind"),
    [
        ("Jarvis, erinnere mich morgen um 18 Uhr daran, meinen DayZ Server zu prüfen.",
         "2026-09-22 18:00", ScheduleKind.ONE_TIME),
        ("Erinnere mich um 18 Uhr an meinen Bot.", "2026-09-21 18:00", ScheduleKind.ONE_TIME),
        ("Erinnere mich in 20 Minuten daran, den Ofen auszuschalten.",
         "2026-09-21 14:50", ScheduleKind.ONE_TIME),
        ("Jeden Freitag um 17 Uhr Server prüfen.", "2026-09-25 17:00", ScheduleKind.WEEKLY),
        ("Jeden Tag um 8 Uhr Bot-Status prüfen.", "2026-09-22 08:00", ScheduleKind.DAILY),
        ("Alle 3 Stunden Serverstatus prüfen.", "2026-09-21 17:30", ScheduleKind.INTERVAL),
        ("Erinnere mich übermorgen daran, das Backup zu prüfen.",
         "2026-09-23 09:00", ScheduleKind.ONE_TIME),
    ],
)
def test_german_reminder_phrases(phrase: str, expected_local: str, kind: ScheduleKind) -> None:
    parsed = parse_schedule(phrase, timezone=TZ, now=NOW)
    assert parsed is not None, phrase
    local = parsed.when.astimezone(ZoneInfo(TZ))
    assert local.strftime("%Y-%m-%d %H:%M") == expected_local
    assert parsed.kind is kind


def test_the_reminder_text_drops_the_request_wrapper() -> None:
    parsed = parse_schedule(
        "Jarvis, erinnere mich morgen um 18 Uhr daran, meinen DayZ Server zu prüfen.",
        timezone=TZ, now=NOW,
    )
    assert "erinnere" not in parsed.text.lower()
    assert "DayZ Server" in parsed.text


def test_a_time_today_that_already_passed_moves_to_tomorrow() -> None:
    parsed = parse_schedule("Erinnere mich um 9 Uhr an das Meeting.", timezone=TZ, now=NOW)
    assert parsed.when.astimezone(ZoneInfo(TZ)).day == 22


def test_phrases_without_a_time_are_rejected() -> None:
    for phrase in ("Wie spät ist es?", "Erzähl mir einen Witz", ""):
        assert parse_schedule(phrase, timezone=TZ, now=NOW) is None


def test_parsing_needs_no_model_or_network() -> None:
    """Spec §97: firing and creating reminders must work offline."""
    assert parse_schedule("in 5 Minuten Kaffee", timezone=TZ, now=NOW) is not None


# --- recurrence -----------------------------------------------------------------------------------


def test_daily_recurrence_keeps_local_wall_clock_time() -> None:
    reminder = {
        "schedule_kind": ScheduleKind.DAILY.value,
        "next_run_at": datetime(2026, 9, 21, 16, 0, tzinfo=UTC).isoformat(),
        "timezone": TZ, "interval_seconds": 86400,
    }
    after = datetime(2026, 9, 21, 16, 5, tzinfo=UTC)
    upcoming = next_occurrence(reminder, after)
    local = upcoming.astimezone(ZoneInfo(TZ))
    assert local.strftime("%H:%M") == "18:00"       # 16:00 UTC is 18:00 in Berlin in summer
    assert local.day == 22


def test_daily_recurrence_survives_a_dst_change() -> None:
    """A 18:00 reminder must stay at 18:00 local time after the clocks change."""
    # 2026-10-25 is the end of summer time in Europe/Berlin.
    reminder = {
        "schedule_kind": ScheduleKind.DAILY.value,
        "next_run_at": datetime(2026, 10, 24, 16, 0, tzinfo=UTC).isoformat(),   # 18:00 local
        "timezone": TZ, "interval_seconds": 86400,
    }
    after = datetime(2026, 10, 24, 16, 5, tzinfo=UTC)
    upcoming = next_occurrence(reminder, after)
    assert upcoming.astimezone(ZoneInfo(TZ)).strftime("%H:%M") == "18:00"


def test_weekly_recurrence_picks_the_configured_weekday() -> None:
    reminder = {
        "schedule_kind": ScheduleKind.WEEKLY.value,
        "next_run_at": datetime(2026, 9, 25, 15, 0, tzinfo=UTC).isoformat(),
        "timezone": TZ, "weekdays": "[4]",
    }
    upcoming = next_occurrence(reminder, datetime(2026, 9, 25, 15, 5, tzinfo=UTC))
    assert upcoming.astimezone(ZoneInfo(TZ)).weekday() == 4


def test_a_one_time_reminder_has_no_next_occurrence() -> None:
    assert next_occurrence({
        "schedule_kind": ScheduleKind.ONE_TIME.value,
        "next_run_at": datetime.now(UTC).isoformat(), "timezone": TZ,
    }) is None


# --- firing and persistence ------------------------------------------------------------------------


async def test_a_reminder_fires_once_and_is_then_disabled(scheduler: Scheduler) -> None:
    await scheduler.create_reminder("Server prüfen", datetime.now(UTC) - timedelta(seconds=10))
    fired = await scheduler.tick()
    assert len(fired) == 1
    assert await scheduler.tick() == []              # it does not fire again
    assert await scheduler.list_reminders() == []    # and is no longer pending


async def test_a_recurring_reminder_reschedules_itself(scheduler: Scheduler) -> None:
    await scheduler.create_reminder(
        "Täglich prüfen", datetime.now(UTC) - timedelta(seconds=5),
        kind=ScheduleKind.DAILY, timezone=TZ, interval_seconds=86400,
    )
    assert len(await scheduler.tick()) == 1
    remaining = await scheduler.list_reminders()
    assert len(remaining) == 1
    assert remaining[0]["fire_count"] == 1
    assert await scheduler.tick() == []


async def test_reminders_survive_a_restart_and_missed_ones_fire(database: Database) -> None:
    """Spec §27: a reminder must not be lost because the PC was switched off."""
    first = Scheduler(database)
    await first.create_reminder("Verpasst", datetime.now(UTC) - timedelta(hours=3))

    restarted = Scheduler(database)      # a fresh instance stands in for a restart
    fired = await restarted.tick()
    assert len(fired) == 1
    assert fired[0]["missed"] is True


async def test_a_future_reminder_does_not_fire(scheduler: Scheduler) -> None:
    await scheduler.create_reminder("Später", datetime.now(UTC) + timedelta(hours=2))
    assert await scheduler.tick() == []
    assert len(await scheduler.list_reminders()) == 1


async def test_creating_a_reminder_from_text(scheduler: Scheduler) -> None:
    result = await scheduler.create_from_text("Erinnere mich in 2 Stunden an den Server",
                                              timezone=TZ)
    assert result is not None
    reminder_id, parsed = result
    assert reminder_id > 0
    assert "Server" in parsed.text
    assert await scheduler.create_from_text("Kein Zeitpunkt hier", timezone=TZ) is None


async def test_deleting_and_disabling_reminders(scheduler: Scheduler) -> None:
    reminder_id = await scheduler.create_reminder("X", datetime.now(UTC) + timedelta(days=1))
    await scheduler.set_reminder_enabled(reminder_id, False)
    assert await scheduler.list_reminders() == []
    assert len(await scheduler.list_reminders(include_disabled=True)) == 1
    assert await scheduler.delete_reminder(reminder_id) is True
    assert await scheduler.delete_reminder(reminder_id) is False


# --- tasks -------------------------------------------------------------------------------------------


async def test_a_task_tracks_its_steps_and_progress(task_manager: TaskManager) -> None:
    task_id = await task_manager.create("Server prüfen", steps=["Status", "Logs", "Bericht"])
    steps = await task_manager.steps(task_id)
    assert len(steps) == 3

    await task_manager.update_step(steps[0]["id"], TaskStatus.DONE)
    task = await task_manager.get(task_id)
    assert task["progress"] == pytest.approx(1 / 3)

    for step in steps[1:]:
        await task_manager.update_step(step["id"], TaskStatus.DONE)
    assert (await task_manager.get(task_id))["progress"] == pytest.approx(1.0)


async def test_cancelling_a_task_signals_its_cancel_event(task_manager: TaskManager) -> None:
    task_id = await task_manager.create("Lange Aufgabe")
    await task_manager.start(task_id)
    handle = task_manager.handle(task_id)
    assert handle is not None and not handle.cancelled

    assert await task_manager.cancel(task_id) is True
    assert handle.cancelled is True
    assert (await task_manager.get(task_id))["status"] == TaskStatus.CANCELLED.value


async def test_cancelling_a_parent_cancels_its_children(task_manager: TaskManager) -> None:
    parent = await task_manager.create("Hauptaufgabe")
    child = await task_manager.create("Teilaufgabe", parent_id=parent)
    await task_manager.start(parent)
    await task_manager.start(child)

    await task_manager.cancel(parent)
    assert (await task_manager.get(child))["status"] == TaskStatus.CANCELLED.value


async def test_run_marks_completion_and_failure(task_manager: TaskManager) -> None:
    async def work() -> str:
        return "fertig"

    ok_id = await task_manager.create("Erfolgreich")
    assert await task_manager.run(ok_id, work()) == "fertig"
    assert (await task_manager.get(ok_id))["status"] == TaskStatus.DONE.value

    async def broken() -> None:
        raise RuntimeError("kaputt")

    fail_id = await task_manager.create("Fehlschlag")
    with pytest.raises(RuntimeError):
        await task_manager.run(fail_id, broken())
    failed = await task_manager.get(fail_id)
    assert failed["status"] == TaskStatus.FAILED.value
    assert "kaputt" in failed["error"]


async def test_stale_tasks_are_cleaned_up_at_startup(task_manager: TaskManager) -> None:
    """A task left RUNNING by a crash must not appear active forever."""
    task_id = await task_manager.create("Unterbrochen")
    await task_manager.start(task_id)
    assert await task_manager.cleanup_stale() >= 1
    assert (await task_manager.get(task_id))["status"] == TaskStatus.FAILED.value


async def test_the_task_tree_includes_steps_and_children(task_manager: TaskManager) -> None:
    parent = await task_manager.create("Haupt", steps=["A"])
    await task_manager.create("Kind", parent_id=parent)
    tree = await task_manager.tree(parent)
    assert len(tree["steps"]) == 1
    assert len(tree["children"]) == 1


async def test_cancel_all_stops_every_running_task(task_manager: TaskManager) -> None:
    ids = [await task_manager.create(f"Aufgabe {i}") for i in range(3)]
    for task_id in ids:
        await task_manager.start(task_id)
    assert await task_manager.cancel_all("Not-Stopp") == 3
    for task_id in ids:
        assert (await task_manager.get(task_id))["status"] == TaskStatus.CANCELLED.value


# --- proactivity -----------------------------------------------------------------------------------------


def settings_with_mode(mode: ProactivityMode) -> Settings:
    settings = Settings()
    settings.assistant.proactivity = mode
    return settings


@pytest.mark.parametrize(
    ("mode", "importance", "expected"),
    [
        (ProactivityMode.OFF, Importance.CRITICAL, False),
        (ProactivityMode.IMPORTANT_ONLY, Importance.NORMAL, False),
        (ProactivityMode.IMPORTANT_ONLY, Importance.IMPORTANT, True),
        (ProactivityMode.NORMAL, Importance.NORMAL, True),
        (ProactivityMode.NORMAL, Importance.CHATTER, False),
        (ProactivityMode.PROACTIVE, Importance.CHATTER, True),
    ],
)
def test_proactivity_thresholds(mode: ProactivityMode, importance: Importance, expected: bool) -> None:
    engine = ProactiveEngine()
    allowed, _ = engine.should_notify(
        Notification("Test", importance), settings_with_mode(mode), now=1000.0
    )
    assert allowed is expected


def test_the_same_message_is_not_repeated() -> None:
    engine = ProactiveEngine()
    settings = settings_with_mode(ProactivityMode.NORMAL)
    notification = Notification("Der Bot ist abgestürzt.", Importance.IMPORTANT, "discord")
    assert engine.notify(notification, settings, now=1000.0) is True
    assert engine.notify(notification, settings, now=1060.0) is False
    # After the dedupe window it may be reported again.
    assert engine.notify(notification, settings, now=1000.0 + 1000) is True


def test_a_flapping_service_cannot_spam_the_user() -> None:
    engine = ProactiveEngine()
    settings = settings_with_mode(ProactivityMode.PROACTIVE)
    delivered = sum(
        engine.notify(Notification(f"Meldung {i}", Importance.NORMAL), settings, now=1000.0 + i)
        for i in range(20)
    )
    assert delivered <= 6


def test_critical_notifications_bypass_the_rate_limit() -> None:
    """Suppressing "your server is down" to satisfy a counter would be the wrong trade."""
    engine = ProactiveEngine()
    settings = settings_with_mode(ProactivityMode.NORMAL)
    for i in range(20):
        engine.notify(Notification(f"Rauschen {i}", Importance.NORMAL), settings, now=1000.0 + i)
    assert engine.notify(
        Notification("Der Server ist offline.", Importance.CRITICAL), settings, now=1020.0
    ) is True


async def test_watchers_are_polled_and_failures_are_contained() -> None:
    engine = ProactiveEngine()

    async def good() -> list[Notification]:
        return [Notification("Alles gut", Importance.IMPORTANT, "health")]

    async def broken() -> list[Notification]:
        raise RuntimeError("watcher kaputt")

    engine.register_watcher("good", good)
    engine.register_watcher("broken", broken)
    delivered = await engine.poll(settings_with_mode(ProactivityMode.NORMAL))
    assert len(delivered) == 1


async def test_the_scheduler_loop_starts_and_stops_cleanly(scheduler: Scheduler) -> None:
    await scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()
