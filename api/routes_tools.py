"""Tool, permission and emergency-stop endpoints (Spec §69, §70, §77, §89)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import get_settings
from core.emergency import emergency
from core.enums import Capability, RiskLevel
from core.errors import JarvisError
from core.logging_setup import get_logger
from core.permissions import permissions
from tools.base import ToolContext
from tools.registry import registry
from tools.sandbox import describe_sandbox

log = get_logger("api.tools")

router = APIRouter(prefix="/api", tags=["tools"])


class ToolExecuteRequest(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    conversation_id: int | None = None


class ConfirmationAnswer(BaseModel):
    id: str
    answer: str = Field(pattern="^(allow_once|allow_always|deny)$")


@router.get("/tools")
async def list_tools() -> dict[str, Any]:
    settings = get_settings()
    return {
        "tools": registry.describe_all(settings),
        "risk_levels": [r.value for r in RiskLevel],
        "capabilities": [c.value for c in Capability],
        "sandbox": describe_sandbox(settings),
    }


@router.post("/tools/execute")
async def execute_tool(body: ToolExecuteRequest) -> dict[str, Any]:
    """Run a tool directly.

    This goes through exactly the same permission, audit and sandbox path as a model-issued
    call — there is no privileged shortcut for the UI.
    """
    try:
        result = await registry.execute(
            body.tool,
            body.arguments,
            context=ToolContext(conversation_id=body.conversation_id, agent="user"),
        )
    except JarvisError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    return {"result": result.to_dict()}


@router.get("/tools/runs")
async def recent_tool_runs(limit: int = 50) -> dict[str, Any]:
    return {"runs": await registry.recent_runs(limit)}


@router.get("/permissions/pending")
async def pending_confirmations() -> dict[str, Any]:
    return {"pending": permissions.pending(), "grants": permissions.grants()}


@router.post("/permissions/answer")
async def answer_confirmation(body: ConfirmationAnswer) -> dict[str, Any]:
    """Answer a pending confirmation (Spec §69)."""
    if not permissions.resolve(body.id, body.answer):
        raise HTTPException(
            status_code=404,
            detail="Diese Anfrage ist nicht mehr offen (bereits beantwortet oder abgelaufen).",
        )
    return {"answered": body.id, "answer": body.answer}


@router.get("/permissions/grants")
async def list_grants() -> dict[str, Any]:
    return {"grants": permissions.grants()}


@router.delete("/permissions/grants")
async def revoke_grant(capability: str | None = None, scope: str | None = None) -> dict[str, Any]:
    """Revoke one remembered "always allow", or all of them."""
    if capability and scope:
        removed = permissions.forget(capability, scope)
        return {"removed": removed, "grants": permissions.grants()}
    permissions.clear_grants()
    return {"removed": True, "grants": []}


@router.get("/emergency")
async def emergency_state() -> dict[str, Any]:
    return emergency.state


@router.post("/emergency/stop")
async def emergency_engage(reason: str = "Über die Oberfläche ausgelöst") -> dict[str, Any]:
    """STOP JARVIS: halt automation, the tool chain and speech output (Spec §70)."""
    return emergency.engage(reason)


@router.post("/emergency/release")
async def emergency_release() -> dict[str, Any]:
    return emergency.release()
