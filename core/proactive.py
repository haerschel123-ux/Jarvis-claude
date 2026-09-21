"""Proactivity engine (Spec §14, §81).

JARVIS may speak up on its own — but only about things that matter, and only as often as the
user allows. Four levels, from silent to chatty, and a rate limit so a flapping service
cannot turn into a stream of notifications.

The engine decides *whether* to notify. What to watch is registered by the subsystems
themselves (integrations, scheduler, health), so this module has no dependency on them.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from core.config import Settings, get_settings
from core.enums import ProactivityMode
from core.events import EventType, event_bus
from core.logging_setup import get_logger

log = get_logger("proactive")


class Importance(IntEnum):
    """How much the user is likely to care."""

    CHATTER = 0      # nice to know, easily ignored
    NORMAL = 1       # ordinary results and status changes
    IMPORTANT = 2    # something the user would want to act on
    CRITICAL = 3     # something is broken or unsafe


# The lowest importance each mode will surface.
THRESHOLDS: dict[ProactivityMode, Importance | None] = {
    ProactivityMode.OFF: None,
    ProactivityMode.IMPORTANT_ONLY: Importance.IMPORTANT,
    ProactivityMode.NORMAL: Importance.NORMAL,
    ProactivityMode.PROACTIVE: Importance.CHATTER,
}

# Do not repeat the same notification within this window.
DEDUPE_SECONDS = 900.0
# Cap on notifications per rolling window, so a flapping service cannot spam the user.
RATE_WINDOW = 300.0
RATE_LIMIT = 6


@dataclass(slots=True)
class Notification:
    message: str
    importance: Importance = Importance.NORMAL
    source: str = "system"
    kind: str = "info"                 # info | ok | warn | error
    key: str = ""                      # dedupe key; defaults to source+message
    action: dict[str, Any] | None = None
    data: dict[str, Any] = field(default_factory=dict)

    def dedupe_key(self) -> str:
        return self.key or f"{self.source}:{self.message[:120]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "importance": int(self.importance),
            "source": self.source,
            "kind": self.kind,
            "action": self.action,
            **self.data,
        }


Watcher = Callable[[], Awaitable[list[Notification]]]


class ProactiveEngine:
    def __init__(self) -> None:
        self._recent: dict[str, float] = {}
        self._sent: list[float] = []
        self._watchers: dict[str, Watcher] = {}
        self._suppressed = 0

    # --- watchers ---------------------------------------------------------------------

    def register_watcher(self, name: str, watcher: Watcher) -> None:
        """Register a source of notifications (integration, monitor, scheduler)."""
        self._watchers[name] = watcher

    def unregister_watcher(self, name: str) -> None:
        self._watchers.pop(name, None)

    def watchers(self) -> list[str]:
        return sorted(self._watchers)

    async def poll(self, settings: Settings | None = None) -> list[Notification]:
        """Ask every watcher and deliver whatever passes the policy."""
        settings = settings or get_settings()
        if settings.assistant.proactivity is ProactivityMode.OFF:
            return []

        delivered: list[Notification] = []
        for name, watcher in list(self._watchers.items()):
            try:
                for notification in await watcher():
                    if self.notify(notification, settings):
                        delivered.append(notification)
            except Exception:
                log.exception("Proaktiver Watcher '%s' ist fehlgeschlagen", name)
        return delivered

    # --- delivery ----------------------------------------------------------------------

    def should_notify(
        self, notification: Notification, settings: Settings | None = None, now: float | None = None
    ) -> tuple[bool, str]:
        """Pure policy decision, so it can be tested without side effects."""
        settings = settings or get_settings()
        moment = now if now is not None else time.time()
        mode = settings.assistant.proactivity

        threshold = THRESHOLDS.get(mode)
        if threshold is None:
            return False, "Proaktivität ist ausgeschaltet"
        if notification.importance < threshold:
            return False, f"unter der Schwelle für Modus {mode.value}"

        key = notification.dedupe_key()
        last = self._recent.get(key)
        if last is not None and (moment - last) < DEDUPE_SECONDS:
            return False, "identische Meldung wurde vor Kurzem schon gesendet"

        # Something critical is always delivered, even past the rate limit: suppressing
        # "your server is down" to keep a counter happy would be the wrong trade.
        if notification.importance is not Importance.CRITICAL:
            recent = [t for t in self._sent if (moment - t) < RATE_WINDOW]
            if len(recent) >= RATE_LIMIT:
                return False, "zu viele Meldungen in kurzer Zeit"

        return True, ""

    def notify(
        self, notification: Notification, settings: Settings | None = None, now: float | None = None
    ) -> bool:
        allowed, reason = self.should_notify(notification, settings, now)
        moment = now if now is not None else time.time()
        if not allowed:
            self._suppressed += 1
            log.debug("Meldung unterdrückt (%s): %s", reason, notification.message[:80])
            return False

        self._recent[notification.dedupe_key()] = moment
        self._sent.append(moment)
        self._sent = [t for t in self._sent if (moment - t) < RATE_WINDOW * 2]

        event_bus.emit(EventType.PROACTIVE_MESSAGE, **notification.to_dict())
        if notification.importance >= Importance.IMPORTANT:
            event_bus.emit(EventType.NOTIFICATION, **notification.to_dict())
        log.info("Proaktive Meldung [%s] %s", notification.source, notification.message[:120])
        return True

    def stats(self) -> dict[str, Any]:
        moment = time.time()
        return {
            "watchers": self.watchers(),
            "recent_window": len([t for t in self._sent if (moment - t) < RATE_WINDOW]),
            "rate_limit": RATE_LIMIT,
            "suppressed_total": self._suppressed,
        }

    def reset(self) -> None:
        self._recent.clear()
        self._sent.clear()
        self._suppressed = 0


proactive = ProactiveEngine()


def notify(
    message: str,
    *,
    importance: Importance = Importance.NORMAL,
    source: str = "system",
    kind: str = "info",
    key: str = "",
    **data: Any,
) -> bool:
    """Convenience wrapper used by the rest of the application."""
    return proactive.notify(
        Notification(message, importance, source, kind, key, data=data)
    )
