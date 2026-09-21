"""In-process asynchronous event bus (Spec §78).

Everything that happens inside JARVIS is announced here: assistant state changes, task
progress, tool calls, permission requests, reminders and integration status. The WebSocket
endpoint at ``/api/events`` simply forwards these to every connected client, so the desktop
UI and any paired mobile client always see the same picture.

Subscribers get their own bounded queue. A slow or stalled client drops its oldest events
instead of blocking the producer — the assistant must never be held up by a UI.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from core.logging_setup import get_logger
from core.redaction import redact_value

log = get_logger("events")

QUEUE_SIZE = 512
HISTORY_SIZE = 400


class EventType:
    """Canonical event names (Spec §78). Plain strings keep the UI contract obvious."""

    # Assistant lifecycle
    ASSISTANT_IDLE = "assistant.idle"
    ASSISTANT_LISTENING = "assistant.listening"
    ASSISTANT_RECOGNIZING = "assistant.recognizing"
    ASSISTANT_THINKING = "assistant.thinking"
    ASSISTANT_ACTING = "assistant.acting"
    ASSISTANT_SPEAKING = "assistant.speaking"
    ASSISTANT_ERROR = "assistant.error"
    ASSISTANT_PAUSED = "assistant.paused"

    # Chat streaming
    CHAT_STARTED = "chat.started"
    CHAT_DELTA = "chat.delta"
    CHAT_COMPLETED = "chat.completed"
    CHAT_FAILED = "chat.failed"

    # Tasks
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_UPDATED = "task.updated"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_CANCELLED = "task.cancelled"

    # Tools
    TOOL_REQUESTED = "tool.requested"
    TOOL_AWAITING_PERMISSION = "tool.awaiting_permission"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    TOOL_FAILED = "tool.failed"
    TOOL_DENIED = "tool.denied"

    # Computer control
    COMPUTER_ACTION = "computer.action"
    COMPUTER_STOPPED = "computer.stopped"

    # Agents
    AGENT_STARTED = "agent.started"
    AGENT_STATUS = "agent.status"
    AGENT_COMPLETED = "agent.completed"

    # Memory, reminders, integrations, notifications
    MEMORY_CREATED = "memory.created"
    MEMORY_UPDATED = "memory.updated"
    MEMORY_DELETED = "memory.deleted"
    REMINDER_TRIGGERED = "reminder.triggered"
    INTEGRATION_STATUS = "integration.status"
    NOTIFICATION = "notification"
    PROACTIVE_MESSAGE = "proactive.message"

    # Voice
    VOICE_WAKE_DETECTED = "voice.wake_detected"
    VOICE_STATE = "voice.state"
    VOICE_TRANSCRIPT = "voice.transcript"

    # System
    SETTINGS_UPDATED = "settings.updated"
    MODELS_REFRESHED = "models.refreshed"
    EMERGENCY_STOP = "emergency.stop"
    HEALTH_UPDATED = "health.updated"


@dataclass(slots=True)
class Event:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "timestamp": self.timestamp,
            "data": self.data,
        }


class Subscription:
    """A single consumer's view of the bus."""

    def __init__(self, bus: EventBus, types: set[str] | None = None) -> None:
        self._bus = bus
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.types = types
        self.dropped = 0

    def offer(self, event: Event) -> None:
        if self.types is not None and event.type not in self.types:
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            # Drop the oldest event so a stalled client cannot block the assistant.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            self.dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(event)

    async def get(self) -> Event:
        return await self._queue.get()

    async def __aiter__(self) -> AsyncIterator[Event]:
        while True:
            yield await self.get()

    def close(self) -> None:
        self._bus.unsubscribe(self)


class EventBus:
    """Fan-out bus with a bounded replay history for late-joining clients."""

    def __init__(self) -> None:
        self._subscribers: list[Subscription] = []
        self._history: deque[Event] = deque(maxlen=HISTORY_SIZE)
        self._handlers: dict[str, list[Callable[[Event], Any]]] = {}

    def subscribe(self, types: set[str] | None = None) -> Subscription:
        sub = Subscription(self, types)
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subscribers:
            self._subscribers.remove(sub)

    def on(self, event_type: str, handler: Callable[[Event], Any]) -> None:
        """Register an in-process handler. Coroutine handlers are scheduled as tasks."""
        self._handlers.setdefault(event_type, []).append(handler)

    def off(self, event_type: str, handler: Callable[[Event], Any]) -> None:
        handlers = self._handlers.get(event_type)
        if handlers and handler in handlers:
            handlers.remove(handler)

    def emit(self, event_type: str, **data: Any) -> Event:
        """Publish an event. Safe to call from synchronous code."""
        event = Event(type=event_type, data=redact_value(data))
        self._history.append(event)
        for sub in list(self._subscribers):
            sub.offer(event)
        for handler in self._handlers.get(event_type, []):
            self._dispatch(handler, event)
        return event

    def _dispatch(self, handler: Callable[[Event], Any], event: Event) -> None:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # No loop running: close the coroutine rather than leaking a warning.
                    result.close()
                else:
                    loop.create_task(result)
        except Exception:
            log.exception("Event handler for %s failed", event.type)

    def history(self, limit: int = 100, types: set[str] | None = None) -> list[dict[str, Any]]:
        items = list(self._history)
        if types is not None:
            items = [e for e in items if e.type in types]
        return [e.to_dict() for e in items[-limit:]]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def clear_history(self) -> None:
        self._history.clear()


event_bus = EventBus()
