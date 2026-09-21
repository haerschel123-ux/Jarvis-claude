"""Permission engine (Spec §15, §16, §68, §69, §90).

Three independent controls combine, and the **strictest one wins**:

1. The global autonomy level (0 read-only … 4 full auto) versus the action's risk level.
2. The per-capability setting (DENY / ASK / ALLOW).
3. Remembered decisions such as "always allow for this folder".

The engine is the single authority. It takes no input from model output or file content, so
no amount of text — in a web page, a file, an e-mail — can widen what JARVIS may do
(Spec §90). An engaged emergency stop denies everything.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.config import Settings, get_settings
from core.emergency import emergency
from core.enums import (
    RISK_ORDER,
    AutonomyLevel,
    Capability,
    PermissionDecision,
    PermissionValue,
    RiskLevel,
)
from core.errors import PermissionDenied
from core.events import EventType, event_bus
from core.logging_setup import audit, get_logger
from core.redaction import redact_value

log = get_logger("permissions")

# The highest risk each autonomy level performs without asking. Anything above it asks;
# anything a level may not do at all is denied.
AUTO_CEILING: dict[AutonomyLevel, RiskLevel | None] = {
    AutonomyLevel.READ_ONLY: RiskLevel.SAFE_READ,
    AutonomyLevel.ASK_EVERYTHING: RiskLevel.SAFE_READ,
    AutonomyLevel.ASK_RISKY: RiskLevel.SAFE_ACTION,
    AutonomyLevel.AUTO_TRUSTED: RiskLevel.SYSTEM_CONTROL,
    AutonomyLevel.FULL_AUTO: RiskLevel.DESTRUCTIVE,
}

# Level 0 is read-only in the strict sense: anything with an effect is refused outright
# rather than offered for confirmation.
DENY_ABOVE: dict[AutonomyLevel, RiskLevel | None] = {
    AutonomyLevel.READ_ONLY: RiskLevel.SAFE_READ,
}

# Running a privileged/administrative action is never automatic, at any level. The user can
# still allow it per request; they cannot switch it to silent.
ALWAYS_CONFIRM = frozenset({RiskLevel.PRIVILEGED})

CONFIRMATION_TIMEOUT = 300.0


@dataclass(slots=True)
class PermissionRequest:
    """One action awaiting a decision."""

    tool: str
    capability: Capability
    risk: RiskLevel
    arguments: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    scope: str = ""            # e.g. a folder, so "always allow here" can be remembered
    agent: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "capability": self.capability.value,
            "risk": self.risk.value,
            "arguments": redact_value(self.arguments),
            "summary": self.summary,
            "scope": self.scope,
            "agent": self.agent,
        }


@dataclass(slots=True)
class Decision:
    decision: PermissionDecision
    reason: str
    request: PermissionRequest
    remembered: bool = False

    @property
    def allowed(self) -> bool:
        return self.decision is PermissionDecision.ALLOWED

    @property
    def denied(self) -> bool:
        return self.decision is PermissionDecision.DENIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "remembered": self.remembered,
            "request": self.request.to_dict(),
        }


@dataclass
class PendingConfirmation:
    id: str
    request: PermissionRequest
    created_at: float
    future: asyncio.Future[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "request": self.request.to_dict(),
        }


class PermissionEngine:
    def __init__(self) -> None:
        # Remembered "always allow" grants: (capability, scope) -> expiry timestamp or None.
        self._grants: dict[tuple[str, str], float | None] = {}
        self._pending: dict[str, PendingConfirmation] = {}

    # --- evaluation -----------------------------------------------------------------

    def evaluate(
        self, request: PermissionRequest, settings: Settings | None = None
    ) -> Decision:
        """Decide without asking anyone. Pure function of policy plus remembered grants."""
        settings = settings or get_settings()

        if emergency.engaged:
            return Decision(
                PermissionDecision.DENIED,
                "Not-Stopp ist aktiv — es werden keine Aktionen ausgeführt.",
                request,
            )

        capability_value = settings.permission_for(request.capability)
        level = settings.assistant.autonomy_level

        # 1. An explicit DENY is final; nothing overrides it.
        if capability_value is PermissionValue.DENY:
            return Decision(
                PermissionDecision.DENIED,
                f"Die Berechtigung '{request.capability.value}' steht auf DENY.",
                request,
            )

        # 2. Read-only refuses anything with an effect outright.
        deny_above = DENY_ABOVE.get(level)
        if deny_above is not None and RISK_ORDER[request.risk] > RISK_ORDER[deny_above]:
            return Decision(
                PermissionDecision.DENIED,
                "Autonomie-Stufe 0 erlaubt nur lesende Aktionen.",
                request,
            )

        # 3. A remembered grant short-circuits the confirmation, but never a DENY and never a
        #    risk level the current autonomy level may not reach.
        if request.risk not in ALWAYS_CONFIRM and self._has_grant(request):
            return Decision(
                PermissionDecision.ALLOWED,
                f"Zuvor dauerhaft erlaubt für '{request.scope or request.tool}'.",
                request,
                remembered=True,
            )

        # 4. Privileged actions always need a fresh confirmation.
        if request.risk in ALWAYS_CONFIRM:
            return Decision(
                PermissionDecision.NEEDS_CONFIRMATION,
                "Aktionen mit Administratorrechten werden immer einzeln bestätigt.",
                request,
            )

        # 5. The per-capability ASK setting forces a confirmation regardless of level.
        if capability_value is PermissionValue.ASK:
            return Decision(
                PermissionDecision.NEEDS_CONFIRMATION,
                f"Die Berechtigung '{request.capability.value}' steht auf ASK.",
                request,
            )

        # 6. Finally the autonomy level decides.
        ceiling = AUTO_CEILING.get(level)
        if ceiling is not None and RISK_ORDER[request.risk] <= RISK_ORDER[ceiling]:
            return Decision(
                PermissionDecision.ALLOWED,
                f"Stufe {level.value} führt '{request.risk.value}' automatisch aus.",
                request,
            )
        return Decision(
            PermissionDecision.NEEDS_CONFIRMATION,
            f"Stufe {level.value} bestätigt '{request.risk.value}' vorher.",
            request,
        )

    # --- remembered grants ----------------------------------------------------------

    def _grant_key(self, request: PermissionRequest) -> tuple[str, str]:
        return (request.capability.value, request.scope or request.tool)

    def _has_grant(self, request: PermissionRequest) -> bool:
        key = self._grant_key(request)
        expiry = self._grants.get(key, "missing")
        if expiry == "missing":
            return False
        if expiry is None:
            return True
        if time.time() > float(expiry):
            self._grants.pop(key, None)
            return False
        return True

    def remember(self, request: PermissionRequest, ttl_seconds: float | None = None) -> None:
        key = self._grant_key(request)
        self._grants[key] = (time.time() + ttl_seconds) if ttl_seconds else None
        log.info("Dauerhaft erlaubt: %s für '%s'", key[0], key[1])
        audit("permission.remembered", capability=key[0], scope=key[1], ttl=ttl_seconds)

    def forget(self, capability: str, scope: str) -> bool:
        return self._grants.pop((capability, scope), "missing") != "missing"

    def clear_grants(self) -> None:
        self._grants.clear()

    def grants(self) -> list[dict[str, Any]]:
        return [
            {"capability": capability, "scope": scope, "expires_at": expiry}
            for (capability, scope), expiry in self._grants.items()
        ]

    # --- interactive confirmation ---------------------------------------------------

    async def request_confirmation(
        self, request: PermissionRequest, timeout: float = CONFIRMATION_TIMEOUT
    ) -> str:
        """Ask the user and wait. Returns 'allow_once', 'allow_always' or 'deny'.

        A timeout counts as a denial: silence must never be read as consent.
        """
        confirmation_id = uuid.uuid4().hex[:12]
        loop = asyncio.get_running_loop()
        pending = PendingConfirmation(
            confirmation_id, request, time.time(), loop.create_future()
        )
        self._pending[confirmation_id] = pending

        event_bus.emit(
            EventType.TOOL_AWAITING_PERMISSION,
            id=confirmation_id,
            **request.to_dict(),
        )
        log.info("Warte auf Bestätigung für %s (%s)", request.tool, request.risk.value)

        try:
            answer = await asyncio.wait_for(pending.future, timeout=timeout)
        except TimeoutError:
            answer = "deny"
            log.info("Bestätigung für %s ist abgelaufen — als Ablehnung gewertet", request.tool)
        except asyncio.CancelledError:
            answer = "deny"
            raise
        finally:
            self._pending.pop(confirmation_id, None)

        if answer == "allow_always":
            self.remember(request)
        return answer

    def resolve(self, confirmation_id: str, answer: str) -> bool:
        """Called by the API when the user clicks a button."""
        pending = self._pending.get(confirmation_id)
        if pending is None or pending.future.done():
            return False
        if answer not in ("allow_once", "allow_always", "deny"):
            raise ValueError(f"unsupported answer: {answer}")
        pending.future.set_result(answer)
        audit(
            "permission.answered",
            tool=pending.request.tool,
            capability=pending.request.capability.value,
            answer=answer,
        )
        return True

    def pending(self) -> list[dict[str, Any]]:
        return [p.to_dict() for p in self._pending.values()]

    def cancel_all_pending(self, reason: str = "abgebrochen") -> int:
        """Deny everything waiting — used by the emergency stop."""
        count = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result("deny")
                count += 1
        if count:
            log.info("%d offene Bestätigung(en) verworfen: %s", count, reason)
        return count

    # --- convenience ----------------------------------------------------------------

    async def authorise(
        self, request: PermissionRequest, settings: Settings | None = None
    ) -> Decision:
        """Evaluate and, if needed, ask. Raises :class:`PermissionDenied` on refusal."""
        return await self.authorise_with_timeout(request, settings)

    async def authorise_with_timeout(
        self,
        request: PermissionRequest,
        settings: Settings | None = None,
        timeout: float = CONFIRMATION_TIMEOUT,
    ) -> Decision:
        decision = self.evaluate(request, settings)

        if decision.decision is PermissionDecision.NEEDS_CONFIRMATION:
            answer = await self.request_confirmation(request, timeout=timeout)
            if answer == "deny":
                decision = Decision(
                    PermissionDecision.DENIED, "Vom Benutzer abgelehnt.", request
                )
            else:
                decision = Decision(
                    PermissionDecision.ALLOWED,
                    "Vom Benutzer bestätigt."
                    + (" Für diesen Bereich dauerhaft." if answer == "allow_always" else ""),
                    request,
                    remembered=answer == "allow_always",
                )

        audit(
            "permission.decided",
            tool=request.tool,
            capability=request.capability.value,
            risk=request.risk.value,
            decision=decision.decision.value,
            reason=decision.reason,
        )

        if decision.denied:
            event_bus.emit(EventType.TOOL_DENIED, tool=request.tool, reason=decision.reason)
            raise PermissionDenied(
                f"{request.tool} denied: {decision.reason}",
                detail=decision.to_dict(),
                user_message=decision.reason,
            )
        return decision


permissions = PermissionEngine()

# The emergency stop must also clear anything waiting for an answer.
emergency.add_listener(lambda: permissions.cancel_all_pending("Not-Stopp"))
