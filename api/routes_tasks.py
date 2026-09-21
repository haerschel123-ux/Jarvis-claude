"""Task and reminder endpoints (Spec §27, §28, §77, §102, §103)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.backup import backups
from core.enums import ScheduleKind
from core.logging_setup import get_logger
from core.proactive import proactive
from core.scheduler import parse_schedule, scheduler
from core.tasks import tasks

log = get_logger("api.tasks")

router = APIRouter(prefix="/api", tags=["tasks"])


class ReminderCreate(BaseModel):
    text: str = Field(min_length=2, max_length=500)
    when: str | None = None                 # ISO timestamp; omit to parse it from `text`
    timezone: str = "UTC"
    schedule_kind: str = ScheduleKind.ONE_TIME.value
    interval_seconds: int | None = None
    weekdays: list[int] | None = None
    project_id: int | None = None


class TaskCreate(BaseModel):
    title: str = Field(min_length=2, max_length=200)
    goal: str = ""
    steps: list[str] = Field(default_factory=list)
    project_id: int | None = None


@router.get("/tasks")
async def list_tasks(limit: int = 50, active_only: bool = False) -> dict[str, Any]:
    return {
        "tasks": await tasks.list(limit, active_only=active_only),
        "running": tasks.running_ids(),
    }


@router.post("/tasks")
async def create_task(body: TaskCreate) -> dict[str, Any]:
    task_id = await tasks.create(
        body.title, goal=body.goal, steps=body.steps or None, project_id=body.project_id
    )
    return {"task": await tasks.tree(task_id)}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int) -> dict[str, Any]:
    tree = await tasks.tree(task_id)
    if tree is None:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    return {"task": tree}


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, reason: str = "vom Benutzer abgebrochen") -> dict[str, Any]:
    if await tasks.get(task_id) is None:
        raise HTTPException(status_code=404, detail="Aufgabe nicht gefunden")
    cancelled = await tasks.cancel(task_id, reason)
    return {"cancelled": cancelled, "task": await tasks.tree(task_id)}


@router.get("/reminders")
async def list_reminders(include_disabled: bool = False) -> dict[str, Any]:
    return {
        "reminders": await scheduler.list_reminders(include_disabled),
        "now": datetime.now(UTC).isoformat(),
    }


@router.post("/reminders")
async def create_reminder(body: ReminderCreate) -> dict[str, Any]:
    """Create a reminder, either from an explicit timestamp or from natural language."""
    if body.when:
        try:
            when = datetime.fromisoformat(body.when.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Ungültiger Zeitpunkt: {exc}") from exc
        try:
            kind = ScheduleKind(body.schedule_kind)
        except ValueError:
            kind = ScheduleKind.ONE_TIME
        reminder_id = await scheduler.create_reminder(
            body.text, when, kind=kind, timezone=body.timezone,
            interval_seconds=body.interval_seconds, weekdays=body.weekdays,
            project_id=body.project_id,
        )
        return {"id": reminder_id, "parsed": None}

    result = await scheduler.create_from_text(
        body.text, timezone=body.timezone, project_id=body.project_id
    )
    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Ich konnte keinen Zeitpunkt erkennen. Nenne eine Uhrzeit, einen Tag "
                   "oder einen Abstand, zum Beispiel: „morgen um 18 Uhr“ oder „in 20 Minuten“.",
        )
    reminder_id, parsed = result
    return {"id": reminder_id, "parsed": parsed.to_dict()}


@router.post("/reminders/parse")
async def preview_reminder(text: str, timezone: str = "UTC") -> dict[str, Any]:
    """Show how a phrase would be interpreted, without creating anything."""
    parsed = parse_schedule(text, timezone=timezone)
    return {"parsed": parsed.to_dict() if parsed else None}


@router.patch("/reminders/{reminder_id}")
async def update_reminder(reminder_id: int, enabled: bool) -> dict[str, Any]:
    await scheduler.set_reminder_enabled(reminder_id, enabled)
    return {"id": reminder_id, "enabled": enabled}


@router.delete("/reminders/{reminder_id}")
async def delete_reminder(reminder_id: int) -> dict[str, Any]:
    if not await scheduler.delete_reminder(reminder_id):
        raise HTTPException(status_code=404, detail="Erinnerung nicht gefunden")
    return {"deleted": reminder_id}


@router.get("/proactive")
async def proactive_state() -> dict[str, Any]:
    return proactive.stats()


@router.get("/backups")
async def list_backups() -> dict[str, Any]:
    return {
        "backups": [info.to_dict() for info in backups.list()],
        "due": await backups.is_due(),
    }


@router.post("/backups")
async def create_backup(label: str = "") -> dict[str, Any]:
    info = await backups.create(label=label)
    return {"backup": info.to_dict()}
