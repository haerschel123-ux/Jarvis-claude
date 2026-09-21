"""Global emergency stop (Spec §70, §103).

One switch that halts everything JARVIS is actively doing: GUI automation, the running tool
chain, speech output and any queued follow-up actions. It deliberately does **not** kill the
LLM backend or the server — the user must still be able to talk to JARVIS and lift the stop.

While the stop is engaged the permission engine denies every action, so nothing can slip
through between the stop being pressed and a task noticing its cancellation.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from typing import Any

from core.events import EventType, event_bus
from core.logging_setup import audit, get_logger

log = get_logger("emergency")


class EmergencyStop:
    """Process-wide stop flag with listeners for subsystems that need to react."""

    def __init__(self) -> None:
        self._engaged = False
        self._lock = threading.RLock()
        self._engaged_at: float | None = None
        self._reason: str = ""
        self._listeners: list[Callable[[], Any]] = []
        self._cancel_scopes: set[asyncio.Event] = set()

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def state(self) -> dict[str, Any]:
        return {
            "engaged": self._engaged,
            "since": self._engaged_at,
            "reason": self._reason,
        }

    def add_listener(self, callback: Callable[[], Any]) -> None:
        """Register a subsystem that must stop (mouse driver, TTS, task runner …)."""
        self._listeners.append(callback)

    def register_scope(self, event: asyncio.Event) -> None:
        """Register a cancellation event that is set when the stop engages."""
        with self._lock:
            self._cancel_scopes.add(event)
            if self._engaged:
                event.set()

    def unregister_scope(self, event: asyncio.Event) -> None:
        with self._lock:
            self._cancel_scopes.discard(event)

    def engage(self, reason: str = "Benutzer hat den Not-Stopp ausgelöst") -> dict[str, Any]:
        with self._lock:
            already = self._engaged
            self._engaged = True
            self._engaged_at = time.time()
            self._reason = reason
            scopes = list(self._cancel_scopes)
            listeners = list(self._listeners)

        for scope in scopes:
            scope.set()
        for listener in listeners:
            try:
                listener()
            except Exception:
                # A failing listener must never prevent the other subsystems from stopping.
                log.exception("Not-Stopp-Listener ist fehlgeschlagen")

        if not already:
            log.warning("NOT-STOPP ausgelöst: %s", reason)
            audit("emergency.stop", reason=reason, result="engaged")
            event_bus.emit(EventType.EMERGENCY_STOP, engaged=True, reason=reason)
            event_bus.emit(EventType.COMPUTER_STOPPED, reason=reason)
        return self.state

    def release(self) -> dict[str, Any]:
        with self._lock:
            was = self._engaged
            self._engaged = False
            self._engaged_at = None
            self._reason = ""
            scopes = list(self._cancel_scopes)
            self._cancel_scopes.clear()
        for scope in scopes:
            scope.clear()
        if was:
            log.info("Not-Stopp aufgehoben")
            audit("emergency.release", result="released")
            event_bus.emit(EventType.EMERGENCY_STOP, engaged=False)
        return self.state

    def check(self) -> None:
        """Raise if the stop is engaged. Call before any action with an effect."""
        if self._engaged:
            from core.errors import EmergencyStopped

            raise EmergencyStopped(f"Emergency stop engaged: {self._reason}")


emergency = EmergencyStop()
