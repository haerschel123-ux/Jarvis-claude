"""Task graph, progress and cancellation (Spec §102, §103).

A task is a tree: a goal, its steps, and sub-tasks. Everything long-running gets one, so the
user can always see what JARVIS is doing and stop it — cancellation propagates through an
``asyncio.Event`` that the tool engine, the agents and the model stream all observe.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from core.emergency import emergency
from core.enums import TERMINAL_TASK_STATUSES, TaskStatus
from core.errors import Cancelled
from core.events import EventType, event_bus
from core.logging_setup import get_logger
from memory.database import Database, db

log = get_logger("tasks")


@dataclass(slots=True)
class RunningTask:
    """The in-process handle for a task that is currently executing."""

    id: int
    title: str
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled(f"Task {self.id} wurde abgebrochen")


class TaskManager:
    def __init__(self, database: Database | None = None) -> None:
        self._db = database or db
        self._running: dict[int, RunningTask] = {}

    # --- lifecycle -------------------------------------------------------------------

    async def create(
        self,
        title: str,
        *,
        goal: str = "",
        intent: str | None = None,
        parent_id: int | None = None,
        project_id: int | None = None,
        conversation_id: int | None = None,
        agent: str | None = None,
        steps: list[str] | None = None,
    ) -> int:
        task_id = await self._db.execute(
            """
            INSERT INTO tasks (title, goal, intent, parent_id, project_id, conversation_id, agent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (title, goal, intent, parent_id, project_id, conversation_id, agent),
        )
        if steps:
            await self.add_steps(task_id, steps)
        event_bus.emit(EventType.TASK_CREATED, id=task_id, title=title, parent_id=parent_id)
        log.info("Aufgabe angelegt: %s (#%d)", title, task_id)
        return task_id

    async def add_steps(self, task_id: int, titles: list[str]) -> list[int]:
        existing = await self._db.fetch_value(
            "SELECT COALESCE(MAX(position), -1) FROM task_steps WHERE task_id = ?", (task_id,)
        )
        start = int(existing or -1) + 1
        ids: list[int] = []
        for offset, title in enumerate(titles):
            ids.append(await self._db.execute(
                "INSERT INTO task_steps (task_id, position, title) VALUES (?, ?, ?)",
                (task_id, start + offset, title),
            ))
        await self._recompute_progress(task_id)
        return ids

    async def start(self, task_id: int) -> None:
        await self._set_status(task_id, TaskStatus.RUNNING, started=True)
        row = await self.get(task_id)
        self._running[task_id] = RunningTask(task_id, (row or {}).get("title", ""))
        emergency.register_scope(self._running[task_id].cancel)
        event_bus.emit(EventType.TASK_STARTED, id=task_id, title=(row or {}).get("title"))

    async def complete(self, task_id: int, result: str = "") -> None:
        await self._db.execute(
            "UPDATE tasks SET status = ?, result = ?, progress = 1.0, "
            "finished_at = datetime('now') WHERE id = ?",
            (TaskStatus.DONE.value, result[:4000], task_id),
        )
        self._release(task_id)
        row = await self.get(task_id)
        event_bus.emit(EventType.TASK_COMPLETED, id=task_id, title=(row or {}).get("title"))

    async def fail(self, task_id: int, error: str) -> None:
        await self._db.execute(
            "UPDATE tasks SET status = ?, error = ?, finished_at = datetime('now') WHERE id = ?",
            (TaskStatus.FAILED.value, error[:2000], task_id),
        )
        self._release(task_id)
        row = await self.get(task_id)
        event_bus.emit(EventType.TASK_FAILED, id=task_id, title=(row or {}).get("title"),
                       error=error[:200])

    async def cancel(self, task_id: int, reason: str = "vom Benutzer abgebrochen") -> bool:
        """Signal cancellation and mark the task. Sub-tasks are cancelled too."""
        running = self._running.get(task_id)
        if running is not None:
            running.cancel.set()
            if running.task is not None and not running.task.done():
                running.task.cancel()

        for child in await self.children(task_id):
            if child["status"] not in {s.value for s in TERMINAL_TASK_STATUSES}:
                await self.cancel(int(child["id"]), reason)

        row = await self.get(task_id)
        if row is None:
            return False
        if row["status"] in {s.value for s in TERMINAL_TASK_STATUSES}:
            return False

        await self._db.execute(
            "UPDATE tasks SET status = ?, error = ?, finished_at = datetime('now') WHERE id = ?",
            (TaskStatus.CANCELLED.value, reason, task_id),
        )
        self._release(task_id)
        event_bus.emit(EventType.TASK_CANCELLED, id=task_id, reason=reason)
        log.info("Aufgabe #%d abgebrochen: %s", task_id, reason)
        return True

    async def await_permission(self, task_id: int) -> None:
        await self._set_status(task_id, TaskStatus.WAITING_PERMISSION)

    def _release(self, task_id: int) -> None:
        running = self._running.pop(task_id, None)
        if running is not None:
            emergency.unregister_scope(running.cancel)

    async def _set_status(self, task_id: int, status: TaskStatus, started: bool = False) -> None:
        if started:
            await self._db.execute(
                "UPDATE tasks SET status = ?, started_at = COALESCE(started_at, datetime('now')) "
                "WHERE id = ?",
                (status.value, task_id),
            )
        else:
            await self._db.execute("UPDATE tasks SET status = ? WHERE id = ?",
                                   (status.value, task_id))
        event_bus.emit(EventType.TASK_UPDATED, id=task_id, status=status.value)

    # --- steps -------------------------------------------------------------------------

    async def update_step(
        self, step_id: int, status: TaskStatus, *, result: str = "", detail: str = ""
    ) -> None:
        finished = status in TERMINAL_TASK_STATUSES
        await self._db.execute(
            "UPDATE task_steps SET status = ?, result = ?, detail = ?, "
            "finished_at = CASE WHEN ? THEN datetime('now') ELSE finished_at END WHERE id = ?",
            (status.value, result[:2000], detail[:1000], int(finished), step_id),
        )
        row = await self._db.fetch_one("SELECT task_id FROM task_steps WHERE id = ?", (step_id,))
        if row:
            await self._recompute_progress(int(row["task_id"]))

    async def _recompute_progress(self, task_id: int) -> None:
        row = await self._db.fetch_one(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'DONE' THEN 1 ELSE 0 END) AS done "
            "FROM task_steps WHERE task_id = ?",
            (task_id,),
        )
        total = int((row or {}).get("total") or 0)
        done = int((row or {}).get("done") or 0)
        progress = (done / total) if total else 0.0
        await self._db.execute("UPDATE tasks SET progress = ? WHERE id = ?", (progress, task_id))
        event_bus.emit(EventType.TASK_UPDATED, id=task_id, progress=round(progress, 2))

    # --- queries -------------------------------------------------------------------------

    async def get(self, task_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT * FROM tasks WHERE id = ?", (task_id,))

    async def steps(self, task_id: int) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM task_steps WHERE task_id = ? ORDER BY position", (task_id,)
        )

    async def children(self, task_id: int) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM tasks WHERE parent_id = ? ORDER BY id", (task_id,)
        )

    async def tree(self, task_id: int, depth: int = 0) -> dict[str, Any] | None:
        """The task with its steps and sub-tasks, for the UI's progress view."""
        row = await self.get(task_id)
        if row is None:
            return None
        row["steps"] = await self.steps(task_id)
        row["children"] = (
            [await self.tree(int(child["id"]), depth + 1) for child in await self.children(task_id)]
            if depth < 4 else []
        )
        row["running"] = task_id in self._running
        return row

    async def list(
        self, limit: int = 50, *, active_only: bool = False, project_id: int | None = None
    ) -> list[dict[str, Any]]:
        conditions: list[str] = ["parent_id IS NULL"]
        params: list[Any] = []
        if active_only:
            placeholders = ",".join("?" * len(TERMINAL_TASK_STATUSES))
            conditions.append(f"status NOT IN ({placeholders})")
            params.extend(s.value for s in TERMINAL_TASK_STATUSES)
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        params.append(limit)
        return await self._db.fetch_all(
            f"SELECT * FROM tasks WHERE {' AND '.join(conditions)} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        )

    def running_ids(self) -> list[int]:
        return sorted(self._running)

    def handle(self, task_id: int) -> RunningTask | None:
        return self._running.get(task_id)

    async def run(self, task_id: int, coroutine) -> Any:  # noqa: ANN001
        """Execute a coroutine as this task, handling completion, failure and cancellation."""
        await self.start(task_id)
        running = self._running[task_id]
        running.task = asyncio.current_task()
        try:
            result = await coroutine
        except (Cancelled, asyncio.CancelledError):
            await self.cancel(task_id, "abgebrochen")
            raise
        except Exception as exc:
            await self.fail(task_id, f"{type(exc).__name__}: {exc}")
            raise
        await self.complete(task_id, str(result) if result is not None else "")
        return result

    async def cancel_all(self, reason: str = "Not-Stopp") -> int:
        count = 0
        for task_id in list(self._running):
            if await self.cancel(task_id, reason):
                count += 1
        return count

    async def cleanup_stale(self) -> int:
        """Mark tasks left RUNNING by a crash as failed, so the UI is not stuck.

        Called at startup: nothing can still be running in a process that just began.
        """
        rows = await self._db.fetch_all(
            "SELECT id, title FROM tasks WHERE status IN ('RUNNING', 'WAITING', 'WAITING_PERMISSION')"
        )
        for row in rows:
            await self._db.execute(
                "UPDATE tasks SET status = ?, error = ?, finished_at = datetime('now') WHERE id = ?",
                (TaskStatus.FAILED.value, "Durch Neustart von JARVIS unterbrochen", row["id"]),
            )
        if rows:
            log.info("%d unterbrochene Aufgabe(n) beim Start aufgeräumt", len(rows))
        return len(rows)


tasks = TaskManager()


def _cancel_everything_on_emergency() -> None:
    """Emergency-stop listener.

    The stop can be triggered from a thread without a running loop (a tray menu, a global
    hotkey), so the cancellation is only scheduled when there is a loop to schedule it on.
    The cancel events themselves are already set synchronously by the emergency stop, so
    running work stops either way.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(tasks.cancel_all("Not-Stopp"))


emergency.add_listener(_cancel_everything_on_emergency)
