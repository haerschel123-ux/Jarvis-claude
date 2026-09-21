"""Memory endpoints (Spec §26, §77): full control over what JARVIS remembers."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config import get_settings
from core.enums import MemoryKind
from core.logging_setup import get_logger
from memory import secrets_filter
from memory.manager import MemoryCandidate, memory_manager
from memory.retrieval import retrieval

log = get_logger("api.memory")

router = APIRouter(prefix="/api/memory", tags=["memory"])


class MemoryCreate(BaseModel):
    content: str = Field(min_length=3, max_length=2000)
    subject: str = Field(default="", max_length=120)
    kind: str = "fact"
    importance: float = Field(default=0.6, ge=0.0, le=1.0)
    project_id: int | None = None


class MemoryUpdate(BaseModel):
    content: str | None = None
    subject: str | None = None
    kind: str | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    pinned: bool | None = None
    disabled: bool | None = None


class MemoryImport(BaseModel):
    memories: list[dict[str, Any]] = Field(default_factory=list)


@router.get("")
async def list_memories(
    query: str = "", kind: str | None = None, limit: int = 100
) -> dict[str, Any]:
    settings = get_settings()
    return {
        "memories": await memory_manager.list(query=query, kind=kind, limit=limit),
        "counts": await memory_manager.counts(),
        "mode": settings.memory.mode.value,
        "kinds": [k.value for k in MemoryKind],
        "secret_filter": settings.memory.secret_filter_enabled,
    }


@router.post("")
async def create_memory(body: MemoryCreate) -> dict[str, Any]:
    """Store a memory manually. The secret filter applies here too (Spec §25)."""
    verdict = secrets_filter.check(body.content, body.subject)
    if not verdict.allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Dieser Inhalt wird nicht gespeichert: {verdict.reason}. "
                   "Zugangsdaten gehören in die Einstellungen, nicht ins Gedächtnis.",
        )
    try:
        kind = MemoryKind(body.kind.lower())
    except ValueError:
        kind = MemoryKind.FACT

    memory_id = await memory_manager.store(MemoryCandidate(
        content=body.content, subject=body.subject or body.content[:60], kind=kind,
        importance=body.importance, source="manual", project_id=body.project_id,
    ))
    return {"memory": await retrieval.by_id(memory_id)}


@router.get("/search")
async def search_memories(q: str, limit: int = 12) -> dict[str, Any]:
    return {"memories": await memory_manager.recall(q, limit)}


@router.get("/{memory_id}")
async def get_memory(memory_id: int) -> dict[str, Any]:
    memory = await retrieval.by_id(memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    return {"memory": memory}


@router.patch("/{memory_id}")
async def update_memory(memory_id: int, body: MemoryUpdate) -> dict[str, Any]:
    if await retrieval.by_id(memory_id) is None:
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    try:
        memory = await memory_manager.update(memory_id, **fields)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"memory": memory}


@router.delete("/{memory_id}")
async def delete_memory(memory_id: int) -> dict[str, Any]:
    if not await memory_manager.delete(memory_id):
        raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
    return {"deleted": memory_id}


@router.get("/export/all")
async def export_memories() -> dict[str, Any]:
    return await memory_manager.export()


@router.post("/import")
async def import_memories(body: MemoryImport) -> dict[str, Any]:
    return await memory_manager.import_(body.model_dump())
