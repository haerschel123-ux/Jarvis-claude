"""Error taxonomy and recovery classification (Spec §101).

Errors carry enough structure for the recovery loop to decide *what to do next* instead of
blindly retrying: is it worth retrying, would a different strategy help, or must JARVIS stop
and report?
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class Recovery(StrEnum):
    RETRY = "retry"                  # transient; the same call may work again
    ALTERNATIVE = "alternative"      # this approach failed; try a different one
    NEEDS_USER = "needs_user"        # only the user can unblock this
    FATAL = "fatal"                  # stop and report


class JarvisError(Exception):
    """Base class for every error JARVIS raises deliberately."""

    recovery: Recovery = Recovery.FATAL
    user_message: str = "Es ist ein Fehler aufgetreten."

    def __init__(self, message: str, *, detail: Any = None, user_message: str | None = None) -> None:
        super().__init__(message)
        self.detail = detail
        if user_message:
            self.user_message = user_message

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": type(self).__name__,
            "message": str(self),
            "user_message": self.user_message,
            "recovery": self.recovery.value,
            "detail": self.detail,
        }


class ConfigurationError(JarvisError):
    recovery = Recovery.NEEDS_USER
    user_message = "Die Konfiguration ist unvollständig."


class ProviderError(JarvisError):
    """A model provider failed."""

    recovery = Recovery.ALTERNATIVE
    user_message = "Der KI-Anbieter hat nicht geantwortet."


class ProviderUnavailable(ProviderError):
    recovery = Recovery.ALTERNATIVE
    user_message = "Der KI-Anbieter ist nicht erreichbar."


class RateLimited(ProviderError):
    recovery = Recovery.RETRY
    user_message = "Das Limit des Anbieters ist erreicht. Ich versuche es gleich erneut."

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class NoModelAvailable(ProviderError):
    recovery = Recovery.NEEDS_USER
    user_message = "Es ist kein passendes Modell verfügbar."


class PaidModelBlocked(NoModelAvailable):
    """Raised when FREE_ONLY is on and only paid models would fit (Spec §9)."""

    recovery = Recovery.NEEDS_USER
    user_message = (
        "Für diese Aufgabe wäre nur ein kostenpflichtiges Modell geeignet. "
        "FREE ONLY ist aktiv, deshalb habe ich nichts gestartet."
    )


class PermissionDenied(JarvisError):
    recovery = Recovery.NEEDS_USER
    user_message = "Diese Aktion ist nicht erlaubt."


class ConfirmationRequired(JarvisError):
    recovery = Recovery.NEEDS_USER
    user_message = "Für diese Aktion brauche ich deine Bestätigung."


class ToolError(JarvisError):
    recovery = Recovery.ALTERNATIVE
    user_message = "Ein Werkzeug ist fehlgeschlagen."


class ToolNotFound(ToolError):
    recovery = Recovery.FATAL


class ValidationError(ToolError):
    recovery = Recovery.ALTERNATIVE
    user_message = "Die Eingaben für dieses Werkzeug waren ungültig."


class SandboxViolation(JarvisError):
    """Path outside the trusted folders, traversal attempt, or a protected location."""

    recovery = Recovery.NEEDS_USER
    user_message = "Dieser Pfad liegt außerhalb der freigegebenen Ordner."


class Cancelled(JarvisError):
    recovery = Recovery.FATAL
    user_message = "Der Vorgang wurde abgebrochen."


class EmergencyStopped(Cancelled):
    user_message = "Not-Stopp ausgelöst. Alle laufenden Aktionen wurden beendet."


class FeatureUnavailable(JarvisError):
    """A capability the current platform or installation genuinely cannot provide (Spec §108)."""

    recovery = Recovery.NEEDS_USER
    user_message = "Diese Funktion ist in dieser Umgebung nicht verfügbar."

    def __init__(self, message: str, *, reason: str = "", how_to_enable: str = "", **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.reason = reason
        self.how_to_enable = how_to_enable

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["reason"] = self.reason
        data["how_to_enable"] = self.how_to_enable
        return data


class IntegrationError(JarvisError):
    recovery = Recovery.ALTERNATIVE
    user_message = "Eine Integration hat einen Fehler gemeldet."
