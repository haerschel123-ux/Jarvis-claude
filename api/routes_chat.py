"""Chat endpoints (Spec §77).

``POST /api/chat/stream`` returns Server-Sent Events so the UI can render tokens as they
arrive; ``POST /api/chat`` is the same turn collected into one JSON response, which is what
the mobile client and automations use.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.assistant import AssistantCore, TurnEvent, assistant
from core.config import get_settings
from core.logging_setup import get_logger
from memory.conversations import conversations

log = get_logger("api.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    conversation_id: int | None = None
    project_id: int | None = None
    model: str | None = None
    images: list[str] = Field(default_factory=list, max_length=8)
    source: str = "text"


def _core() -> AssistantCore:
    return assistant


async def _event_stream(request: ChatRequest) -> AsyncIterator[str]:
    """Render turn events as SSE frames."""
    try:
        async for event in _core().run_turn(
            request.message,
            conversation_id=request.conversation_id,
            images=request.images,
            project_id=request.project_id,
            explicit_model=request.model,
            source=request.source,
        ):
            yield _sse(event)
    except asyncio.CancelledError:
        # The client went away mid-stream; nothing to report.
        raise
    except Exception as exc:
        log.exception("Chat-Stream abgebrochen")
        yield _sse(TurnEvent("turn.failed", {
            "error": type(exc).__name__,
            "user_message": "Der Chat-Stream ist unerwartet abgebrochen.",
        }))
    yield "data: [DONE]\n\n"


def _sse(event: TurnEvent) -> str:
    payload = json.dumps({"type": event.type, "data": event.data}, ensure_ascii=False, default=str)
    return f"data: {payload}\n\n"


@router.post("/stream")
async def chat_stream(request: ChatRequest) -> StreamingResponse:
    return StreamingResponse(
        _event_stream(request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # keep proxies from buffering the stream
        },
    )


@router.post("")
async def chat(request: ChatRequest) -> dict[str, Any]:
    """Non-streaming variant: the same turn, collected."""
    text_parts: list[str] = []
    result: dict[str, Any] = {"text": "", "events": []}
    failed: dict[str, Any] | None = None

    async for event in _core().run_turn(
        request.message,
        conversation_id=request.conversation_id,
        images=request.images,
        project_id=request.project_id,
        explicit_model=request.model,
        source=request.source,
    ):
        if event.type == "delta":
            text_parts.append(event.data.get("text", ""))
        elif event.type == "turn.failed":
            failed = event.data
        elif event.type in ("turn.completed", "model.selected", "tool.result", "turn.started"):
            result["events"].append({"type": event.type, "data": event.data})

    result["text"] = "".join(text_parts)
    if failed is not None:
        result["error"] = failed
        result["text"] = result["text"] or failed.get("user_message", "")
    return result


@router.get("/conversations")
async def list_conversations(limit: int = 50, project_id: int | None = None) -> dict[str, Any]:
    return {"conversations": await conversations.list(limit, project_id=project_id)}


@router.post("/conversations")
async def create_conversation(title: str = "", project_id: int | None = None) -> dict[str, Any]:
    conversation_id = await conversations.create(title, project_id=project_id)
    return {"id": conversation_id}


@router.get("/conversations/{conversation_id}")
async def get_conversation(conversation_id: int, limit: int = 200) -> dict[str, Any]:
    conversation = await conversations.get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Unterhaltung nicht gefunden")
    return {
        "conversation": conversation,
        "messages": await conversations.messages(conversation_id, limit),
        "usage": await conversations.usage_summary(conversation_id),
    }


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: int,
    title: str | None = None,
    pinned: bool | None = None,
    archived: bool | None = None,
) -> dict[str, Any]:
    if await conversations.get(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Unterhaltung nicht gefunden")
    if title is not None:
        await conversations.rename(conversation_id, title)
    if pinned is not None:
        await conversations.set_flag(conversation_id, "pinned", pinned)
    if archived is not None:
        await conversations.set_flag(conversation_id, "archived", archived)
    return {"conversation": await conversations.get(conversation_id)}


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(conversation_id: int) -> dict[str, Any]:
    await conversations.delete(conversation_id)
    return {"deleted": conversation_id}


@router.post("/conversations/{conversation_id}/truncate")
async def truncate_conversation(conversation_id: int, after_message_id: int) -> dict[str, Any]:
    """Drop everything after a message so the user can edit and resend it (Spec §59)."""
    if await conversations.get(conversation_id) is None:
        raise HTTPException(status_code=404, detail="Unterhaltung nicht gefunden")
    await conversations.truncate_after(conversation_id, after_message_id)
    return {"messages": await conversations.messages(conversation_id)}


@router.get("/defaults")
async def chat_defaults() -> dict[str, Any]:
    """What the composer needs to render before the first message."""
    settings = get_settings()
    return {
        "assistant_name": settings.assistant.name,
        "language": settings.assistant.language,
        "free_only": settings.models.free_only,
        "router_mode": settings.models.router_mode.value,
        "coding_mode": settings.assistant.coding_mode.value,
        "max_output_tokens": settings.models.max_output_tokens,
    }
