"""Event bus fan-out, filtering, redaction and backpressure (Spec §78)."""

from __future__ import annotations

import asyncio

from core.events import QUEUE_SIZE, EventBus, EventType


async def test_subscribers_receive_events() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    bus.emit(EventType.ASSISTANT_THINKING, model="openrouter/free")
    event = await asyncio.wait_for(sub.get(), timeout=1)
    assert event.type == EventType.ASSISTANT_THINKING
    assert event.data["model"] == "openrouter/free"


async def test_filtered_subscription_only_sees_its_types() -> None:
    bus = EventBus()
    sub = bus.subscribe({EventType.TOOL_COMPLETED})
    bus.emit(EventType.ASSISTANT_THINKING)
    bus.emit(EventType.TOOL_COMPLETED, tool="read_file")
    event = await asyncio.wait_for(sub.get(), timeout=1)
    assert event.type == EventType.TOOL_COMPLETED


async def test_event_payloads_are_redacted() -> None:
    """A credential must not reach the UI just because a tool put it in an event."""
    bus = EventBus()
    sub = bus.subscribe()
    bus.emit(EventType.TOOL_COMPLETED, tool="connect", api_key="sk-or-v1-leaked1234567890")
    event = await asyncio.wait_for(sub.get(), timeout=1)
    assert "sk-or-v1-leaked1234567890" not in str(event.data)


async def test_slow_subscriber_drops_events_instead_of_blocking() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    for index in range(QUEUE_SIZE + 50):
        bus.emit(EventType.CHAT_DELTA, index=index)
    assert sub.dropped > 0
    event = await asyncio.wait_for(sub.get(), timeout=1)
    assert event.type == EventType.CHAT_DELTA


async def test_handlers_run_and_failures_do_not_propagate() -> None:
    bus = EventBus()
    seen: list[str] = []
    bus.on(EventType.REMINDER_TRIGGERED, lambda e: seen.append(e.data["text"]))
    bus.on(EventType.REMINDER_TRIGGERED, lambda e: 1 / 0)  # must not break the emit
    bus.emit(EventType.REMINDER_TRIGGERED, text="Server prüfen")
    assert seen == ["Server prüfen"]


async def test_async_handlers_are_scheduled() -> None:
    bus = EventBus()
    seen: list[str] = []

    async def handler(event) -> None:  # noqa: ANN001
        seen.append(event.type)

    bus.on(EventType.TASK_COMPLETED, handler)
    bus.emit(EventType.TASK_COMPLETED)
    await asyncio.sleep(0)
    assert seen == [EventType.TASK_COMPLETED]


async def test_history_replays_recent_events() -> None:
    bus = EventBus()
    bus.emit(EventType.TASK_CREATED, id=1)
    bus.emit(EventType.TASK_COMPLETED, id=1)
    types = [e["type"] for e in bus.history()]
    assert types == [EventType.TASK_CREATED, EventType.TASK_COMPLETED]
    assert [e["type"] for e in bus.history(types={EventType.TASK_CREATED})] == [EventType.TASK_CREATED]


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    sub = bus.subscribe()
    sub.close()
    bus.emit(EventType.ASSISTANT_IDLE)
    assert bus.subscriber_count == 0
