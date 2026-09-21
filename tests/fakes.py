"""Deterministic test doubles.

Nothing here talks to the network: provider HTTP is served by ``httpx.MockTransport`` and
model behaviour by :class:`ScriptedProvider`, so tests are fast and repeatable.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from providers.base import (
    ChatProvider,
    Message,
    ModelInfo,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)

# --- realistic provider payloads ---------------------------------------------------------

OPENROUTER_MODELS: dict[str, Any] = {
    "data": [
        {
            "id": "meta-llama/llama-3.3-70b-instruct:free",
            "name": "Llama 3.3 70B Instruct (free)",
            "context_length": 131072,
            "pricing": {"prompt": "0", "completion": "0", "request": "0", "image": "0"},
            "architecture": {"modality": "text->text", "input_modalities": ["text"],
                             "output_modalities": ["text"], "tokenizer": "Llama3"},
            "supported_parameters": ["tools", "tool_choice", "temperature", "max_tokens"],
            "top_provider": {"context_length": 131072, "max_completion_tokens": 4096},
        },
        {
            "id": "google/gemini-flash-1.5:free",
            "name": "Gemini Flash 1.5 (free)",
            "context_length": 1000000,
            "pricing": {"prompt": "0", "completion": "0"},
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "response_format", "structured_outputs"],
        },
        {
            "id": "openai/gpt-4o",
            "name": "GPT-4o",
            "context_length": 128000,
            "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "structured_outputs", "reasoning"],
        },
        {
            "id": "openrouter/auto",
            "name": "Auto Router",
            "pricing": {"prompt": "-1", "completion": "-1"},
        },
        {
            # Deliberately incomplete: no pricing, no capabilities, no context.
            "id": "mystery/model-x",
            "name": "Mystery Model",
        },
    ]
}

OLLAMA_TAGS: dict[str, Any] = {
    "models": [
        {
            "name": "qwen2.5-coder:7b",
            "model": "qwen2.5-coder:7b",
            "size": 4_700_000_000,
            "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"},
        },
        {
            "name": "llava:13b",
            "model": "llava:13b",
            "size": 8_000_000_000,
            "details": {"parameter_size": "13B", "quantization_level": "Q4_0"},
        },
    ]
}

OLLAMA_CAPABILITIES: dict[str, list[str]] = {
    "qwen2.5-coder:7b": ["completion", "tools"],
    "llava:13b": ["completion", "vision"],
}


def openrouter_transport(
    *,
    models: dict[str, Any] | None = None,
    stream_frames: list[str] | None = None,
    status: int = 200,
    error_body: dict[str, Any] | None = None,
) -> httpx.MockTransport:
    """A MockTransport that answers OpenRouter's /models and /chat/completions."""
    payload = models if models is not None else OPENROUTER_MODELS
    frames = stream_frames if stream_frames is not None else [
        ": OPENROUTER PROCESSING",
        'data: {"choices":[{"delta":{"role":"assistant","content":"Hallo"}}]}',
        'data: {"choices":[{"delta":{"content":", ich bin JARVIS."}}]}',
        'data: {"choices":[{"finish_reason":"stop"}],'
        '"usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19,"cost":0}}',
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if status >= 400:
            return httpx.Response(status, json=error_body or {"error": {"message": "nope"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("/chat/completions"):
            body = "\n".join(frames) + "\n"
            return httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(404, json={"error": "unknown endpoint"})

    return httpx.MockTransport(handler)


def ollama_transport(
    *,
    tags: dict[str, Any] | None = None,
    chat_frames: list[str] | None = None,
    reachable: bool = True,
) -> httpx.MockTransport:
    """A MockTransport that answers Ollama's /api/tags, /api/show and /api/chat."""
    tag_payload = tags if tags is not None else OLLAMA_TAGS
    frames = chat_frames if chat_frames is not None else [
        json.dumps({"message": {"role": "assistant", "content": "Guten "}, "done": False}),
        json.dumps({"message": {"role": "assistant", "content": "Abend."}, "done": False}),
        json.dumps({"done": True, "done_reason": "stop", "prompt_eval_count": 9, "eval_count": 4}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if not reachable:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/tags":
            return httpx.Response(200, json=tag_payload)
        if path == "/api/show":
            model = json.loads(request.content or b"{}").get("model", "")
            return httpx.Response(200, json={"capabilities": OLLAMA_CAPABILITIES.get(model, [])})
        if path == "/api/chat":
            return httpx.Response(200, content=("\n".join(frames) + "\n").encode())
        return httpx.Response(404, json={"error": "unknown endpoint"})

    return httpx.MockTransport(handler)


# --- scripted provider -------------------------------------------------------------------


class ScriptedProvider(ChatProvider):
    """A provider that replays a fixed script — used to test the layers above the network."""

    name = "scripted"
    label = "Scripted"

    def __init__(
        self,
        models: list[ModelInfo] | None = None,
        *,
        script: list[list[StreamChunk]] | None = None,
        is_local: bool = False,
        available: bool = True,
    ) -> None:
        self._models = models or []
        self._script = script or []
        self.is_local = is_local
        self._available = available
        self.calls: list[dict[str, Any]] = []

    async def is_available(self) -> tuple[bool, str]:
        return self._available, "Verbunden" if self._available else "Nicht verfügbar"

    async def list_models(self) -> list[ModelInfo]:
        return list(self._models)

    async def stream_chat(
        self,
        messages: Sequence[Message],
        model: str,
        *,
        tools: Sequence[ToolSpec] | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {"model": model, "messages": list(messages), "tools": list(tools or []), **kwargs}
        )
        index = min(len(self.calls) - 1, len(self._script) - 1) if self._script else -1
        chunks = self._script[index] if index >= 0 else [
            StreamChunk(type="text", text="ok"),
            StreamChunk(type="usage", usage=Usage(1, 1, 2, 0.0)),
            StreamChunk(type="done", finish_reason="stop"),
        ]
        for chunk in chunks:
            yield chunk


def text_turn(text: str, finish: str = "stop") -> list[StreamChunk]:
    return [
        StreamChunk(type="text", text=text),
        StreamChunk(type="usage", usage=Usage(10, 5, 15, 0.0)),
        StreamChunk(type="done", finish_reason=finish),
    ]


def tool_turn(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> list[StreamChunk]:
    return [
        StreamChunk(type="tool_call", tool_call=ToolCall(call_id, name, arguments)),
        StreamChunk(type="usage", usage=Usage(10, 5, 15, 0.0)),
        StreamChunk(type="done", finish_reason="tool_calls"),
    ]


def free_model(model_id: str, **kwargs: Any) -> ModelInfo:
    defaults = {
        "provider": "scripted", "id": model_id, "name": model_id,
        "context_length": 32768, "price_prompt": "0", "price_completion": "0",
        "is_free": True, "supports_tools": True,
    }
    defaults.update(kwargs)
    return ModelInfo(**defaults)


def paid_model(model_id: str, **kwargs: Any) -> ModelInfo:
    defaults = {
        "provider": "scripted", "id": model_id, "name": model_id,
        "context_length": 128000, "price_prompt": "0.000005", "price_completion": "0.00001",
        "is_free": False, "supports_tools": True,
    }
    defaults.update(kwargs)
    return ModelInfo(**defaults)
