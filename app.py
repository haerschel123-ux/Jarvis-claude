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

from api import ws_events  # noqa: E402
from core import health  # noqa: E402
from core.config import get_settings, settings_store  # noqa: E402
from core.events import EventType, event_bus  # noqa: E402
from core.logging_setup import get_logger, setup_logging  # noqa: E402
from core.paths import PATHS  # noqa: E402
from core.platform_info import summary as platform_summary  # noqa: E402
from memory.database import db  # noqa: E402

VERSION = "0.1.0"

log = get_logger("app")

START_TIME = time.time()


async def _startup_background(app: FastAPI) -> None:
    """Checks that may be slow. Failures are logged, never fatal."""
    try:
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

    app.state.settings = settings
    app.state.health = {"overall": "unknown", "checks": {}, "errors": [], "warnings": []}
    app.state.background = asyncio.create_task(_startup_background(app))

    event_bus.emit(EventType.ASSISTANT_IDLE, state="IDLE", version=VERSION)
    log.info("JARVIS ist online auf http://%s:%s", settings.server.host, settings.server.port)

    try:
        yield
    finally:
        task: asyncio.Task | None = getattr(app.state, "background", None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await db.close()
        log.info("JARVIS wurde beendet")


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

    app.include_router(ws_events.router)
    _register_core_routes(app)
    _mount_web_ui(app)
    return app


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
