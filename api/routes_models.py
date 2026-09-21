"""Model and provider endpoints (Spec §8, §77)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from core.config import get_settings
from core.logging_setup import get_logger
from core.model_profiles import Requirements
from core.model_router import DEFAULT_PRIORITIES
from core.model_router import router as model_router
from providers.catalog import catalog

log = get_logger("api.models")

router = APIRouter(prefix="/api", tags=["models"])


@router.get("/providers")
async def list_providers() -> dict[str, Any]:
    catalog.configure()
    statuses = await catalog.provider_statuses()
    return {
        "providers": [s.to_dict() for s in statuses],
        "errors": catalog.last_errors,
    }


@router.get("/models")
async def list_models(
    free: bool | None = None,
    local: bool | None = None,
    provider: str | None = None,
    tools: bool = False,
    vision: bool = False,
    structured: bool = False,
    reasoning: bool = False,
    min_context: int | None = None,
    search: str | None = None,
) -> dict[str, Any]:
    """The catalogue, with the UI filters from Spec §8."""
    settings = get_settings()
    # FREE_ONLY is a policy, not merely a filter: when it is on, paid models are not listed
    # as selectable unless the caller explicitly asks to see them.
    free_only = settings.models.free_only if free is None else free
    models = await catalog.filter(
        free_only=free_only,
        local_only=bool(local),
        provider=provider,
        needs_tools=tools,
        needs_vision=vision,
        needs_structured=structured,
        needs_reasoning=reasoning,
        min_context=min_context,
        search=search,
    )
    return {
        "models": [m.to_dict() for m in models],
        "total": await catalog.count_all(),
        "free_only": free_only,
        "stale": await catalog.is_stale(),
    }


@router.post("/models/refresh")
async def refresh_models(force: bool = True) -> dict[str, Any]:
    catalog.configure()
    return await catalog.refresh(force=force)


@router.get("/models/{provider}/{model_id:path}")
async def get_model(provider: str, model_id: str) -> dict[str, Any]:
    model = await catalog.get(f"{provider}:{model_id}")
    if model is None:
        raise HTTPException(status_code=404, detail="Modell nicht gefunden")
    return {"model": model.to_dict()}


@router.post("/models/select")
async def preview_selection(
    task_kind: str = "chat",
    tools: bool = False,
    vision: bool = False,
    reasoning: bool = False,
    min_context: int | None = None,
) -> dict[str, Any]:
    """Show which model the router would pick, and why (Spec §117)."""
    selection = await model_router.select(
        task_kind,
        requirements=Requirements(
            needs_tools=tools, needs_vision=vision,
            needs_reasoning=reasoning, min_context=min_context,
        ),
    )
    return selection.to_dict()


@router.get("/models/priorities")
async def priorities() -> dict[str, Any]:
    settings = get_settings()
    merged = dict(DEFAULT_PRIORITIES)
    merged.update(settings.models.priorities)
    return {"priorities": merged, "defaults": DEFAULT_PRIORITIES}
