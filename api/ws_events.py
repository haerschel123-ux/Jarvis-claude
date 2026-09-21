"""WebSocket endpoint that mirrors the event bus to every connected client (Spec §77, §78).

A client may optionally filter the stream and ask for the recent history on connect, which
is what the activity panel does after a page reload.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from core.events import Event, EventType, event_bus
from core.logging_setup import get_logger

log = get_logger("ws")

router = APIRouter()

HEARTBEAT_SECONDS = 25.0


@router.websocket("/api/events")
async def events_socket(
    websocket: WebSocket,
    types: str | None = Query(default=None, description="Comma-separated event types to filter"),
    history: int = Query(default=50, ge=0, le=400),
) -> None:
    await websocket.accept()
    wanted = {t.strip() for t in types.split(",") if t.strip()} if types else None
    subscription = event_bus.subscribe(wanted)

    try:
        await websocket.send_text(
            json.dumps(
                {"type": "connection.ready", "data": {"history": event_bus.history(history, wanted)}}
            )
        )
        pump = asyncio.create_task(_pump(websocket, subscription))
        reader = asyncio.create_task(_drain_client(websocket))
        done, pending = await asyncio.wait({pump, reader}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                task.result()
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("Event socket closed unexpectedly", exc_info=True)
    finally:
        subscription.close()


async def _pump(websocket: WebSocket, subscription) -> None:
    """Forward events, with a periodic heartbeat so idle proxies do not drop the socket."""
    while True:
        try:
            event: Event = await asyncio.wait_for(subscription.get(), timeout=HEARTBEAT_SECONDS)
        except TimeoutError:
            await websocket.send_text(json.dumps({"type": "heartbeat", "data": {}}))
            continue
        await websocket.send_text(json.dumps(event.to_dict(), default=str))


async def _drain_client(websocket: WebSocket) -> None:
    """Read and discard client frames.

    The event socket is server-to-client only; commands go through the REST API so they pass
    the same permission checks. Reading keeps disconnects detectable.
    """
    while True:
        message = await websocket.receive_text()
        if message == "ping":
            await websocket.send_text(json.dumps({"type": "pong", "data": {}}))


def emit_state(state: str, **data) -> None:
    """Convenience used by the assistant core to publish its current state."""
    mapping = {
        "IDLE": EventType.ASSISTANT_IDLE,
        "LISTENING": EventType.ASSISTANT_LISTENING,
        "RECOGNIZING": EventType.ASSISTANT_RECOGNIZING,
        "THINKING": EventType.ASSISTANT_THINKING,
        "ACTING": EventType.ASSISTANT_ACTING,
        "SPEAKING": EventType.ASSISTANT_SPEAKING,
        "ERROR": EventType.ASSISTANT_ERROR,
        "PAUSED": EventType.ASSISTANT_PAUSED,
    }
    event_bus.emit(mapping.get(state, EventType.ASSISTANT_IDLE), state=state, **data)
