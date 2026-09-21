"""JARVIS application entry point.

Starts the FastAPI server, runs database migrations, registers health checks and serves the
web UI. Slow or optional subsystems are started in the background so the window appears fast
(Spec §82); none of them may prevent startup (Spec §114).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import AsyncIterator
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()  # development convenience; the packaged app uses the OS credential store

from agents.orchestrator import Orchestrator  # noqa: E402
from api import (  # noqa: E402
    routes_chat,
    routes_memory,
    routes_models,
    routes_settings,
    routes_tasks,
    routes_tools,
    ws_events,
)
from core import health  # noqa: E402
from core.assistant import assistant  # noqa: E402
from core.backup import backups  # noqa: E402
from core.config import get_settings, settings_store  # noqa: E402
from core.errors import (  # noqa: E402
    ConfigurationError,
    FeatureUnavailable,
    JarvisError,
    NoModelAvailable,
    PermissionDenied,
    ProviderUnavailable,
    RateLimited,
)
from core.events import EventType, event_bus  # noqa: E402
from core.logging_setup import get_logger, setup_logging  # noqa: E402
from core.paths import PATHS  # noqa: E402
from core.platform_info import summary as platform_summary  # noqa: E402
from core.scheduler import scheduler  # noqa: E402
from core.tasks import tasks  # noqa: E402
from memory.database import db  # noqa: E402
from memory.manager import memory_manager  # noqa: E402
from providers.catalog import catalog  # noqa: E402
from tools import register_default_tools  # noqa: E402
from tools.registry import RegistryToolExecutor  # noqa: E402
from tools.registry import registry as tool_registry  # noqa: E402

VERSION = "0.1.0"

log = get_logger("app")

START_TIME = time.time()


async def _recall_memories(query: str, limit: int) -> list[dict[str, Any]]:
    """Memory hook: what JARVIS already knows that is relevant to this message."""
    try:
        return await memory_manager.recall(query, limit)
    except Exception:
        log.exception("Gedächtnisabruf fehlgeschlagen")
        return []


async def _capture_memories(context) -> None:  # noqa: ANN001 - the assistant's TurnContext
    """Memory hook: run the pipeline over the finished exchange."""
    await memory_manager.capture_exchange(
        context.user_message,
        context.answer,
        conversation_id=context.conversation_id,
        project_id=context.project_id,
        settings=context.settings,
    )


async def _startup_background(app: FastAPI) -> None:
    """Checks that may be slow. Failures are logged, never fatal."""
    try:
        # Refresh the model catalogue only when it is stale, so startup stays fast and a
        # provider outage never delays the window appearing (Spec §82).
        try:
            if await catalog.is_stale():
                await catalog.refresh()
                event_bus.emit(EventType.MODELS_REFRESHED, total=await catalog.count_all())
        except Exception:
            log.exception("Modellkatalog konnte beim Start nicht aktualisiert werden")

        try:
            if await backups.is_due():
                await backups.create(label="auto")
        except Exception:
            log.exception("Automatisches Backup fehlgeschlagen")

        result = await health.run_all()
        app.state.health = result
        event_bus.emit(EventType.HEALTH_UPDATED, overall=result["overall"],
                       warnings=result["warnings"], errors=result["errors"])
        if result["errors"]:
            log.warning("Health problems detected: %s", ", ".join(result["errors"]))
        elif result["warnings"]:
            log.info("Health warnings: %s", ", ".join(result["warnings"]))
    except Exception:
        log.exception("Background startup checks failed")


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    debug = os.environ.get("JARVIS_DEBUG", "0") not in ("0", "", "false", "False")
    setup_logging(debug=debug)
    PATHS.ensure()

    settings = settings_store.load()
    log.info("JARVIS %s startet (Assistent: %s, Basis: %s)", VERSION, settings.assistant.name, PATHS.base)

    await db.connect()
    applied = await db.migrate()
    if applied:
        log.info("Datenbankmigrationen angewendet: %s", applied)

    health.register_builtin_checks()
    catalog.configure(settings)

    # Tools are registered once and handed to the assistant core, which then routes every
    # model-issued tool call through the registry's permission and audit path.
    register_default_tools()
    assistant.set_tool_executor(RegistryToolExecutor())
    assistant.set_memory_hooks(_recall_memories, _capture_memories)
    assistant.set_orchestrator(Orchestrator(tool_registry=tool_registry))

    # Anything left RUNNING by a crash is not running now; mark it so the UI is not stuck.
    await tasks.cleanup_stale()
    # The scheduler fires reminders that came due while JARVIS was off (Spec §27).
    await scheduler.start()
    health.register_check("providers", _provider_health)

    app.state.settings = settings
    app.state.health = {"overall": "unknown", "checks": {}, "errors": [], "warnings": []}
    app.state.background = asyncio.create_task(_startup_background(app))

    event_bus.emit(EventType.ASSISTANT_IDLE, state="IDLE", version=VERSION)
    log.info("JARVIS ist online auf http://%s:%s", settings.server.host, settings.server.port)

    try:
        yield
    finally:
        await scheduler.stop()
        task: asyncio.Task | None = getattr(app.state, "background", None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await catalog.close()
        await db.close()
        log.info("JARVIS wurde beendet")


async def _provider_health() -> health.HealthResult:
    """Report provider reachability (Spec §83) without failing startup when one is down."""
    statuses = await catalog.provider_statuses()
    detail = {s.name: {"available": s.available, "reason": s.reason} for s in statuses}
    if not statuses:
        return health.HealthResult(
            "providers", True, "warn", "Kein KI-Anbieter konfiguriert", detail
        )
    reachable = [s for s in statuses if s.available]
    if not reachable:
        return health.HealthResult(
            "providers", False, "error",
            "Kein KI-Anbieter erreichbar: "
            + "; ".join(f"{s.label} ({s.reason})" for s in statuses),
            detail,
        )
    if len(reachable) < len(statuses):
        return health.HealthResult(
            "providers", True, "warn",
            "Nicht erreichbar: "
            + ", ".join(f"{s.label} ({s.reason})" for s in statuses if not s.available),
            detail,
        )
    return health.HealthResult(
        "providers", True, "ok", ", ".join(f"{s.label}: {s.reason}" for s in statuses), detail
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="JARVIS",
        version=VERSION,
        description="Persönlicher KI-Assistent mit Sprache, Gedächtnis, Agenten und PC-Steuerung",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # The UI is served from the same origin. CORS stays closed by default; remote companion
    # access is granted explicitly through device pairing instead (Spec §54, §92).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[],
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _register_exception_handlers(app)
    app.include_router(ws_events.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_models.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_tools.router)
    app.include_router(routes_memory.router)
    app.include_router(routes_tasks.router)
    _register_core_routes(app)
    _mount_web_ui(app)
    return app


# JarvisError subclasses carry a user-facing explanation; a bare 500 would throw it away.
ERROR_STATUS: dict[type[JarvisError], int] = {
    ConfigurationError: 400,
    PermissionDenied: 403,
    NoModelAvailable: 409,
    FeatureUnavailable: 501,
    RateLimited: 429,
    ProviderUnavailable: 503,
}


def _status_for(exc: JarvisError) -> int:
    for error_type, status in ERROR_STATUS.items():
        if isinstance(exc, error_type):
            return status
    return 500


def _register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(JarvisError)
    async def jarvis_error_handler(request, exc: JarvisError):  # noqa: ANN001
        status = _status_for(exc)
        if status >= 500:
            log.error("%s: %s", type(exc).__name__, exc)
        else:
            log.info("%s: %s", type(exc).__name__, exc)
        return JSONResponse(exc.to_dict(), status_code=status)


def _register_core_routes(app: FastAPI) -> None:
    @app.get("/api/health", tags=["system"])
    async def health_endpoint(refresh: bool = False) -> dict[str, Any]:
        """Liveness plus the detailed subsystem report (Spec §83)."""
        if refresh or getattr(app.state, "health", {}).get("overall") == "unknown":
            app.state.health = await health.run_all()
        return {
            "status": "ok",
            "version": VERSION,
            "uptime_seconds": round(time.time() - START_TIME, 1),
            "health": app.state.health,
        }

    @app.get("/api/status", tags=["system"])
    async def status_endpoint() -> dict[str, Any]:
        """Everything the dashboard needs for its header and widgets (Spec §57)."""
        settings = get_settings()
        return {
            "version": VERSION,
            "assistant": {
                "name": settings.assistant.name,
                "state": "IDLE",
                "language": settings.assistant.language,
                "autonomy_level": settings.assistant.autonomy_level.value,
                "proactivity": settings.assistant.proactivity.value,
            },
            "models": {
                "router_mode": settings.models.router_mode.value,
                "free_only": settings.models.free_only,
                "offline_mode": settings.models.offline_mode,
            },
            "voice": {
                "mode": settings.voice.mode.value,
                "wake_words": settings.voice.wake_words,
                "microphone_active": False,
            },
            "health": getattr(app.state, "health", {}).get("overall", "unknown"),
            "first_run_completed": settings.first_run_completed,
            "uptime_seconds": round(time.time() - START_TIME, 1),
            "event_subscribers": event_bus.subscriber_count,
        }

    @app.get("/api/platform", tags=["system"])
    async def platform_endpoint() -> dict[str, Any]:
        """Honest capability report for this machine (Spec §94, §108)."""
        return platform_summary()

    @app.get("/api/events/history", tags=["system"])
    async def event_history(limit: int = 100) -> dict[str, Any]:
        return {"events": event_bus.history(limit)}


def _mount_web_ui(app: FastAPI) -> None:
    web_dir = PATHS.web
    if not web_dir.exists():
        log.warning("Web-Verzeichnis %s fehlt — die Oberfläche wird nicht ausgeliefert", web_dir)
        return

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(web_dir / "index.html")

    @app.exception_handler(404)
    async def spa_fallback(request, exc):  # noqa: ANN001
        """Client-side routes fall back to index.html; API 404s stay 404s."""
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        index_file = web_dir / "index.html"
        if index_file.exists():
            return FileResponse(index_file)
        return JSONResponse({"detail": "Not Found"}, status_code=404)

    app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")


app = create_app()


def main() -> None:
    import uvicorn

    settings = settings_store.load()
    uvicorn.run(
        app,
        host=settings.server.host,
        port=settings.server.port,
        log_config=None,  # JARVIS configures logging itself
        access_log=False,
    )


if __name__ == "__main__":
    main()
