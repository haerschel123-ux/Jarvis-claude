"""Health monitoring (Spec §83).

A registry of named checks. Later stages register their own (providers, voice, integrations)
so this module never has to import them — which keeps startup fast and avoids import cycles.

Checks run concurrently with a per-check timeout, because one unreachable service must not
delay the others (Spec §82: slow checks in parallel).
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from core.logging_setup import get_logger
from core.paths import PATHS
from core.platform_info import capabilities, module_available

log = get_logger("health")

CHECK_TIMEOUT = 6.0


@dataclass(slots=True)
class HealthResult:
    name: str
    ok: bool
    status: str                       # "ok" | "warn" | "error" | "disabled"
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
            "duration_ms": self.duration_ms,
        }


CheckFn = Callable[[], Awaitable[HealthResult]]

_checks: dict[str, CheckFn] = {}


def register_check(name: str, fn: CheckFn) -> None:
    """Register (or replace) a named health check."""
    _checks[name] = fn


def unregister_check(name: str) -> None:
    _checks.pop(name, None)


def registered_checks() -> list[str]:
    return sorted(_checks)


async def _run_one(name: str, fn: CheckFn) -> HealthResult:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(fn(), timeout=CHECK_TIMEOUT)
    except TimeoutError:
        result = HealthResult(name, False, "error", f"Zeitüberschreitung nach {CHECK_TIMEOUT:.0f}s")
    except Exception as exc:
        log.debug("Health check %s raised", name, exc_info=True)
        result = HealthResult(name, False, "error", f"{type(exc).__name__}: {exc}")
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    result.name = name
    return result


async def run_all() -> dict[str, Any]:
    """Run every registered check concurrently and summarise the outcome."""
    names = list(_checks)
    results = await asyncio.gather(*(_run_one(n, _checks[n]) for n in names))
    by_name = {r.name: r.to_dict() for r in results}
    errors = [r.name for r in results if r.status == "error"]
    warnings = [r.name for r in results if r.status == "warn"]
    if errors:
        overall = "error"
    elif warnings:
        overall = "warn"
    else:
        overall = "ok"
    return {
        "overall": overall,
        "errors": errors,
        "warnings": warnings,
        "checks": by_name,
        "checked_at": time.time(),
    }


# --- built-in checks --------------------------------------------------------------------


async def check_database() -> HealthResult:
    from memory.database import db

    if not db.is_connected:
        return HealthResult("database", False, "error", "Keine Verbindung zur Datenbank")
    version = await db.schema_version()
    size_mb = round(db.path.stat().st_size / 1024**2, 2) if db.path.exists() else 0.0
    return HealthResult(
        "database", True, "ok", f"Schema-Version {version}",
        {"path": str(db.path), "schema_version": version, "size_mb": size_mb},
    )


async def check_disk_space() -> HealthResult:
    try:
        usage = shutil.disk_usage(PATHS.base)
    except OSError as exc:
        return HealthResult("disk_space", False, "error", str(exc))
    free_gb = usage.free / 1024**3
    if free_gb < 1:
        return HealthResult("disk_space", False, "error", f"Nur noch {free_gb:.1f} GB frei")
    if free_gb < 5:
        return HealthResult("disk_space", True, "warn", f"Wenig Speicher: {free_gb:.1f} GB frei")
    return HealthResult(
        "disk_space", True, "ok", f"{free_gb:.1f} GB frei",
        {"free_gb": round(free_gb, 1), "total_gb": round(usage.total / 1024**3, 1)},
    )


async def check_git() -> HealthResult:
    path = shutil.which("git")
    if not path:
        return HealthResult(
            "git", True, "warn", "git wurde nicht gefunden — Git-Funktionen sind deaktiviert"
        )
    proc = await asyncio.create_subprocess_exec(
        path, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return HealthResult("git", True, "ok", out.decode().strip(), {"path": path})


async def check_voice_stack() -> HealthResult:
    caps = capabilities()
    parts = {
        "speech_to_text": caps["speech_to_text"]["available"],
        "microphone": caps["microphone"]["available"],
        "wake_word": caps["wake_word"]["available"],
        "text_to_speech": caps["text_to_speech"]["available"],
    }
    missing = [k for k, v in parts.items() if not v]
    if not missing:
        return HealthResult("voice", True, "ok", "Sprachstack vollständig", parts)
    return HealthResult(
        "voice", True, "warn",
        "Sprachfunktionen deaktiviert: " + ", ".join(missing),
        {**parts, "fix": "pip install -r requirements-voice.txt"},
    )


async def check_desktop_control() -> HealthResult:
    caps = capabilities()
    parts = {k: caps[k]["available"] for k in ("screen_capture", "mouse_keyboard", "window_automation")}
    missing = [k for k, v in parts.items() if not v]
    if not missing:
        return HealthResult("desktop_control", True, "ok", "PC-Steuerung verfügbar", parts)
    reasons = {k: caps[k]["reason"] for k in missing}
    return HealthResult(
        "desktop_control", True, "warn",
        "PC-Steuerung eingeschränkt: " + ", ".join(missing),
        {**parts, "reasons": reasons},
    )


async def check_credential_store() -> HealthResult:
    from core.secrets import secret_store

    backend = secret_store.backend_name
    if backend.startswith("keyring"):
        return HealthResult("credentials", True, "ok", f"Betriebssystem-Speicher: {backend}")
    return HealthResult(
        "credentials", True, "warn",
        "Kein Betriebssystem-Speicher verfügbar — Secrets liegen in einer Datei mit 0600-Rechten",
        {"backend": backend, "path": str(secret_store.file_path),
         "fix": "keyring installieren" if not module_available("keyring") else ""},
    )


def register_builtin_checks() -> None:
    register_check("database", check_database)
    register_check("disk_space", check_disk_space)
    register_check("git", check_git)
    register_check("voice", check_voice_stack)
    register_check("desktop_control", check_desktop_control)
    register_check("credentials", check_credential_store)
