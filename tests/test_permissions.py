"""Permission engine: the three controls and their interaction (Spec §15, §16, §68, §70)."""

from __future__ import annotations

import asyncio

import pytest

from core.config import Settings
from core.emergency import EmergencyStop
from core.enums import (
    AutonomyLevel,
    Capability,
    PermissionDecision,
    PermissionValue,
    RiskLevel,
)
from core.errors import PermissionDenied
from core.permissions import PermissionEngine, PermissionRequest


def settings_at(level: AutonomyLevel, **capabilities: PermissionValue) -> Settings:
    settings = Settings()
    settings.assistant.autonomy_level = level
    for name, value in capabilities.items():
        settings.permissions[name] = value
    return settings


def request(
    capability: Capability = Capability.FILE_WRITE,
    risk: RiskLevel = RiskLevel.WRITE,
    scope: str = "D:/Projects",
) -> PermissionRequest:
    return PermissionRequest("demo_tool", capability, risk, summary="demo", scope=scope)


@pytest.fixture
def engine() -> PermissionEngine:
    return PermissionEngine()


# --- autonomy levels ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("level", "risk", "expected"),
    [
        # Level 0 is strictly read-only: writes are refused, not offered for confirmation.
        (AutonomyLevel.READ_ONLY, RiskLevel.SAFE_READ, PermissionDecision.ALLOWED),
        (AutonomyLevel.READ_ONLY, RiskLevel.SAFE_ACTION, PermissionDecision.DENIED),
        (AutonomyLevel.READ_ONLY, RiskLevel.WRITE, PermissionDecision.DENIED),
        # Level 1 asks before anything that is not a plain read.
        (AutonomyLevel.ASK_EVERYTHING, RiskLevel.SAFE_READ, PermissionDecision.ALLOWED),
        (AutonomyLevel.ASK_EVERYTHING, RiskLevel.SAFE_ACTION, PermissionDecision.NEEDS_CONFIRMATION),
        # Level 2 runs safe actions, confirms real changes.
        (AutonomyLevel.ASK_RISKY, RiskLevel.SAFE_ACTION, PermissionDecision.ALLOWED),
        (AutonomyLevel.ASK_RISKY, RiskLevel.WRITE, PermissionDecision.NEEDS_CONFIRMATION),
        # Level 3 acts within trusted areas but confirms destructive and outward-facing work.
        (AutonomyLevel.AUTO_TRUSTED, RiskLevel.WRITE, PermissionDecision.ALLOWED),
        (AutonomyLevel.AUTO_TRUSTED, RiskLevel.SYSTEM_CONTROL, PermissionDecision.ALLOWED),
        (AutonomyLevel.AUTO_TRUSTED, RiskLevel.DESTRUCTIVE, PermissionDecision.NEEDS_CONFIRMATION),
        # Level 4 is broadly autonomous.
        (AutonomyLevel.FULL_AUTO, RiskLevel.DESTRUCTIVE, PermissionDecision.ALLOWED),
    ],
)
def test_autonomy_level_matrix(
    engine: PermissionEngine, level: AutonomyLevel, risk: RiskLevel, expected: PermissionDecision
) -> None:
    settings = settings_at(level, **{c.value: PermissionValue.ALLOW for c in Capability})
    assert engine.evaluate(request(risk=risk), settings).decision is expected


def test_privileged_always_confirms_even_at_full_auto(engine: PermissionEngine) -> None:
    """Running something as administrator is never silent, at any level."""
    settings = settings_at(
        AutonomyLevel.FULL_AUTO, **{Capability.TERMINAL_ADMIN.value: PermissionValue.ALLOW}
    )
    decision = engine.evaluate(
        request(Capability.TERMINAL_ADMIN, RiskLevel.PRIVILEGED), settings
    )
    assert decision.decision is PermissionDecision.NEEDS_CONFIRMATION


# --- capability matrix --------------------------------------------------------------------


def test_deny_beats_every_autonomy_level(engine: PermissionEngine) -> None:
    settings = settings_at(
        AutonomyLevel.FULL_AUTO, **{Capability.FILE_WRITE.value: PermissionValue.DENY}
    )
    decision = engine.evaluate(request(), settings)
    assert decision.decision is PermissionDecision.DENIED
    assert "DENY" in decision.reason


def test_ask_capability_forces_confirmation_at_high_autonomy(engine: PermissionEngine) -> None:
    """The stricter of the two controls wins (Spec §16: in addition to the global mode)."""
    settings = settings_at(
        AutonomyLevel.FULL_AUTO, **{Capability.FILE_WRITE.value: PermissionValue.ASK}
    )
    assert engine.evaluate(request(), settings).decision is PermissionDecision.NEEDS_CONFIRMATION


def test_allow_capability_does_not_override_a_low_level(engine: PermissionEngine) -> None:
    settings = settings_at(
        AutonomyLevel.READ_ONLY, **{Capability.FILE_WRITE.value: PermissionValue.ALLOW}
    )
    assert engine.evaluate(request(), settings).decision is PermissionDecision.DENIED


# --- remembered grants --------------------------------------------------------------------


def test_remembered_grant_skips_the_confirmation(engine: PermissionEngine) -> None:
    settings = settings_at(AutonomyLevel.ASK_RISKY)
    assert engine.evaluate(request(), settings).decision is PermissionDecision.NEEDS_CONFIRMATION
    engine.remember(request())
    decision = engine.evaluate(request(), settings)
    assert decision.decision is PermissionDecision.ALLOWED
    assert decision.remembered is True


def test_a_grant_is_scoped_to_its_folder(engine: PermissionEngine) -> None:
    settings = settings_at(AutonomyLevel.ASK_RISKY)
    engine.remember(request(scope="D:/Projects"))
    assert engine.evaluate(request(scope="D:/Projects"), settings).allowed
    assert not engine.evaluate(request(scope="D:/Other"), settings).allowed


def test_a_grant_cannot_resurrect_a_denied_capability(engine: PermissionEngine) -> None:
    engine.remember(request())
    settings = settings_at(
        AutonomyLevel.FULL_AUTO, **{Capability.FILE_WRITE.value: PermissionValue.DENY}
    )
    assert engine.evaluate(request(), settings).decision is PermissionDecision.DENIED


def test_a_grant_never_covers_a_privileged_action(engine: PermissionEngine) -> None:
    privileged = request(Capability.TERMINAL_ADMIN, RiskLevel.PRIVILEGED)
    engine.remember(privileged)
    settings = settings_at(
        AutonomyLevel.FULL_AUTO, **{Capability.TERMINAL_ADMIN.value: PermissionValue.ALLOW}
    )
    assert engine.evaluate(privileged, settings).decision is PermissionDecision.NEEDS_CONFIRMATION


def test_grants_can_be_revoked(engine: PermissionEngine) -> None:
    engine.remember(request())
    assert engine.forget(Capability.FILE_WRITE.value, "D:/Projects") is True
    assert engine.grants() == []


# --- interactive confirmation ---------------------------------------------------------------


async def test_confirmation_allows_when_the_user_says_yes(engine: PermissionEngine) -> None:
    settings = settings_at(AutonomyLevel.ASK_RISKY)
    task = asyncio.create_task(engine.authorise(request(), settings))
    await asyncio.sleep(0)
    pending = engine.pending()
    assert len(pending) == 1
    assert engine.resolve(pending[0]["id"], "allow_once") is True
    decision = await task
    assert decision.allowed


async def test_confirmation_denial_raises(engine: PermissionEngine) -> None:
    settings = settings_at(AutonomyLevel.ASK_RISKY)
    task = asyncio.create_task(engine.authorise(request(), settings))
    await asyncio.sleep(0)
    engine.resolve(engine.pending()[0]["id"], "deny")
    with pytest.raises(PermissionDenied):
        await task


async def test_allow_always_is_remembered(engine: PermissionEngine) -> None:
    settings = settings_at(AutonomyLevel.ASK_RISKY)
    task = asyncio.create_task(engine.authorise(request(), settings))
    await asyncio.sleep(0)
    engine.resolve(engine.pending()[0]["id"], "allow_always")
    await task
    # The next identical request runs without asking.
    assert engine.evaluate(request(), settings).allowed


async def test_silence_counts_as_denial(engine: PermissionEngine) -> None:
    """A confirmation that times out must never be treated as consent."""
    settings = settings_at(AutonomyLevel.ASK_RISKY)
    with pytest.raises(PermissionDenied):
        await engine.authorise_with_timeout(request(), settings, timeout=0.05)


async def test_unknown_confirmation_id_is_rejected(engine: PermissionEngine) -> None:
    assert engine.resolve("does-not-exist", "allow_once") is False


# --- emergency stop -------------------------------------------------------------------------


def test_emergency_stop_denies_everything(monkeypatch) -> None:
    """While stopped, even a read a level-4 user allowed is refused (Spec §70)."""
    stop = EmergencyStop()
    engine = PermissionEngine()
    monkeypatch.setattr("core.permissions.emergency", stop)
    settings = settings_at(
        AutonomyLevel.FULL_AUTO, **{c.value: PermissionValue.ALLOW for c in Capability}
    )
    assert engine.evaluate(request(risk=RiskLevel.SAFE_READ), settings).allowed
    stop.engage("test")
    assert engine.evaluate(request(risk=RiskLevel.SAFE_READ), settings).denied
    stop.release()
    assert engine.evaluate(request(risk=RiskLevel.SAFE_READ), settings).allowed


def test_emergency_stop_notifies_listeners_once() -> None:
    stop = EmergencyStop()
    calls: list[int] = []
    stop.add_listener(lambda: calls.append(1))
    stop.engage("first")
    stop.engage("second")          # already engaged: listeners still run, state stays engaged
    assert stop.engaged is True
    assert len(calls) == 2
    stop.release()
    assert stop.engaged is False


def test_a_failing_listener_does_not_block_the_stop() -> None:
    stop = EmergencyStop()
    reached: list[str] = []
    stop.add_listener(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    stop.add_listener(lambda: reached.append("second"))
    stop.engage("test")
    assert stop.engaged is True
    assert reached == ["second"]


async def test_emergency_stop_sets_registered_cancel_scopes() -> None:
    stop = EmergencyStop()
    scope = asyncio.Event()
    stop.register_scope(scope)
    assert not scope.is_set()
    stop.engage("test")
    assert scope.is_set()
