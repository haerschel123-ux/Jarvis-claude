"""Settings, secrets and first-run endpoints (Spec §62, §77, §93)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import Settings, settings_store
from core.enums import AutonomyLevel, Capability, PermissionValue
from core.events import EventType, event_bus
from core.logging_setup import get_logger
from core.platform_info import summary as platform_summary
from core.secrets import SECRET_ENV_MAP, secret_store
from providers.catalog import catalog

log = get_logger("api.settings")

router = APIRouter(prefix="/api", tags=["settings"])


class SettingsPatch(BaseModel):
    """A partial settings update; only the provided keys change."""

    patch: dict[str, Any] = Field(default_factory=dict)


class SecretUpdate(BaseModel):
    name: str
    value: str = ""


@router.get("/settings")
async def get_settings_endpoint() -> dict[str, Any]:
    settings = settings_store.load()
    return {
        "settings": settings.model_dump(mode="json"),
        # Secrets are described, never returned (Spec §91).
        "secrets": secret_store.describe_all(),
        "secret_backend": secret_store.backend_name,
        "capabilities": [c.value for c in Capability],
        "permission_values": [p.value for p in PermissionValue],
        "autonomy_levels": [
            {"value": level.value, "name": level.name} for level in AutonomyLevel
        ],
    }


@router.put("/settings")
async def update_settings(body: SettingsPatch) -> dict[str, Any]:
    if not body.patch:
        raise HTTPException(status_code=400, detail="Leere Änderung")
    if "permissions" in body.patch and not isinstance(body.patch["permissions"], dict):
        raise HTTPException(status_code=400, detail="permissions muss ein Objekt sein")
    try:
        settings = settings_store.update(body.patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Ungültige Einstellung: {exc}") from exc

    # Provider configuration may have changed; rebuild the clients so the next call uses it.
    catalog.configure(settings)
    event_bus.emit(EventType.SETTINGS_UPDATED, changed=sorted(body.patch))
    return {"settings": settings.model_dump(mode="json")}


@router.post("/settings/reset")
async def reset_settings() -> dict[str, Any]:
    """Restore defaults. Secrets are untouched — resetting settings must not log the user out."""
    settings = Settings()
    settings_store.save(settings)
    settings_store.reload()
    catalog.configure(settings)
    event_bus.emit(EventType.SETTINGS_UPDATED, changed=["*"])
    return {"settings": settings.model_dump(mode="json")}


@router.get("/secrets")
async def list_secrets() -> dict[str, Any]:
    return {"secrets": secret_store.describe_all(), "backend": secret_store.backend_name}


@router.put("/secrets")
async def set_secret(body: SecretUpdate) -> dict[str, Any]:
    if body.name not in SECRET_ENV_MAP:
        raise HTTPException(status_code=400, detail=f"Unbekanntes Geheimnis: {body.name}")
    secret_store.set(body.name, body.value)
    catalog.configure()
    log.info("Zugangsdaten für %s aktualisiert", body.name)   # the value itself is never logged
    return {"secret": secret_store.describe(body.name)}


@router.delete("/secrets/{name}")
async def delete_secret(name: str) -> dict[str, Any]:
    if name not in SECRET_ENV_MAP:
        raise HTTPException(status_code=400, detail=f"Unbekanntes Geheimnis: {name}")
    secret_store.delete(name)
    catalog.configure()
    return {"secret": secret_store.describe(name)}


@router.get("/permissions")
async def get_permissions() -> dict[str, Any]:
    settings = settings_store.load()
    return {
        "autonomy_level": settings.assistant.autonomy_level.value,
        "permissions": {k: v.value for k, v in settings.permissions.items()},
        "trusted_folders": settings.security.trusted_folders,
    }


@router.put("/permissions")
async def set_permissions(body: dict[str, Any]) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    if "autonomy_level" in body:
        patch["assistant"] = {"autonomy_level": body["autonomy_level"]}
    if "permissions" in body:
        unknown = set(body["permissions"]) - {c.value for c in Capability}
        if unknown:
            raise HTTPException(status_code=400, detail=f"Unbekannte Capabilities: {sorted(unknown)}")
        patch["permissions"] = body["permissions"]
    if "trusted_folders" in body:
        patch["security"] = {"trusted_folders": body["trusted_folders"]}
    if not patch:
        raise HTTPException(status_code=400, detail="Leere Änderung")
    settings = settings_store.update(patch)
    event_bus.emit(EventType.SETTINGS_UPDATED, changed=["permissions"])
    return {
        "autonomy_level": settings.assistant.autonomy_level.value,
        "permissions": {k: v.value for k, v in settings.permissions.items()},
        "trusted_folders": settings.security.trusted_folders,
    }


@router.get("/setup")
async def setup_state() -> dict[str, Any]:
    """Everything the first-run wizard needs in one call (Spec §93, §94)."""
    settings = settings_store.load()
    platform = platform_summary()
    catalog.configure(settings)
    statuses = await catalog.provider_statuses()
    return {
        "first_run_completed": settings.first_run_completed,
        "assistant": settings.assistant.model_dump(mode="json"),
        "providers": [s.to_dict() for s in statuses],
        "secrets": secret_store.describe_all(),
        "hardware": platform["hardware"],
        "capabilities": platform["capabilities"],
        "recommendations": platform["recommendations"],
        "model_count": await catalog.count_all(),
    }


@router.post("/setup/complete")
async def complete_setup() -> dict[str, Any]:
    settings = settings_store.update({"first_run_completed": True})
    return {"first_run_completed": settings.first_run_completed}
