"""Shared parsing for OpenAI-compatible chat APIs.

OpenRouter, Ollama's ``/v1`` endpoint and any custom OpenAI-compatible server all speak the
same wire format, so the SSE decoding and the streaming tool-call assembly live here once.

Verified against the OpenRouter API reference (see ``docs/VERIFIED_APIS.md``):
streamed chunks arrive as ``data: {...}`` lines terminated by ``data: [DONE]``, and comment
lines beginning with ``:`` are keep-alives that must be ignored.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from providers.base import StreamChunk, ToolCall, Usage


@dataclass
class _PartialToolCall:
    """Tool calls stream in fragments; arguments arrive as a partial JSON string."""

    id: str = ""
    name: str = ""
    arguments: str = ""

    def finish(self, index: int) -> ToolCall | None:
        if not self.name:
            return None
        try:
            parsed = json.loads(self.arguments) if self.arguments.strip() else {}
        except json.JSONDecodeError:
            # A truncated or malformed argument blob is reported as an error argument rather
            # than silently dropped, so the tool layer can reject it with a clear message.
            parsed = {"__malformed_arguments__": self.arguments}
        if not isinstance(parsed, dict):
            parsed = {"value": parsed}
        return ToolCall(id=self.id or f"call_{index}", name=self.name, arguments=parsed)


@dataclass
class StreamState:
    """Accumulates streamed deltas into finished chunks."""

    tool_calls: dict[int, _PartialToolCall] = field(default_factory=dict)
    finish_reason: str | None = None
    usage: Usage | None = None

    def ingest(self, payload: dict[str, Any]) -> list[StreamChunk]:
        """Turn one decoded SSE payload into zero or more chunks."""
        chunks: list[StreamChunk] = []

        if error := payload.get("error"):
            message = error.get("message") if isinstance(error, dict) else str(error)
            return [StreamChunk(type="error", error=str(message))]

        if usage := payload.get("usage"):
            self.usage = parse_usage(usage)

        for choice in payload.get("choices") or []:
            delta = choice.get("delta") or choice.get("message") or {}

            content = delta.get("content")
            if isinstance(content, str) and content:
                chunks.append(StreamChunk(type="text", text=content))
            elif isinstance(content, list):
                # Some providers send structured content parts even while streaming.
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        chunks.append(StreamChunk(type="text", text=part["text"]))

            # Reasoning tokens are surfaced separately so the UI can show "thinking" status
            # without mixing them into the answer (Spec §117).
            reasoning = delta.get("reasoning")
            if isinstance(reasoning, str) and reasoning:
                chunks.append(StreamChunk(type="reasoning", text=reasoning))

            for raw_call in delta.get("tool_calls") or []:
                index = raw_call.get("index", 0)
                partial = self.tool_calls.setdefault(index, _PartialToolCall())
                if raw_call.get("id"):
                    partial.id = raw_call["id"]
                function = raw_call.get("function") or {}
                if function.get("name"):
                    partial.name = function["name"]
                if function.get("arguments"):
                    partial.arguments += function["arguments"]

            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]

        return chunks

    def finalise(self) -> list[StreamChunk]:
        """Emit assembled tool calls, usage and the terminating chunk."""
        chunks: list[StreamChunk] = []
        for index in sorted(self.tool_calls):
            call = self.tool_calls[index].finish(index)
            if call is not None:
                chunks.append(StreamChunk(type="tool_call", tool_call=call))
        if self.usage is not None:
            chunks.append(StreamChunk(type="usage", usage=self.usage))
        chunks.append(StreamChunk(type="done", finish_reason=self.finish_reason))
        return chunks


def parse_usage(raw: dict[str, Any]) -> Usage:
    cost = raw.get("cost")
    if cost is None and isinstance(raw.get("cost_details"), dict):
        cost = raw["cost_details"].get("upstream_inference_cost")
    return Usage(
        prompt_tokens=_as_int(raw.get("prompt_tokens")),
        completion_tokens=_as_int(raw.get("completion_tokens")),
        total_tokens=_as_int(raw.get("total_tokens")),
        cost_usd=_as_float(cost),
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


async def iter_sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    """Decode an SSE line stream into JSON payloads, stopping at ``[DONE]``."""
    async for line in lines:
        line = line.strip()
        if not line or line.startswith(":"):
            continue  # blank line or keep-alive comment
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue  # a partial frame; the next line carries the rest
        if isinstance(payload, dict):
            yield payload


def parse_non_streaming(payload: dict[str, Any]) -> list[StreamChunk]:
    """Convert a complete (non-streamed) response into the same chunk sequence."""
    state = StreamState()
    chunks = state.ingest(payload)
    if chunks and chunks[0].type == "error":
        return chunks
    return chunks + state.finalise()
