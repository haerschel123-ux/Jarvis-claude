"""Ollama provider for local models (Spec §10).

Uses Ollama's **native** API rather than its OpenAI-compatible ``/v1`` shim, because only the
native endpoints expose what JARVIS needs:

* ``GET  /api/tags`` — installed models
* ``POST /api/show`` — a ``capabilities`` array (``completion``/``vision``/``tools``/``thinking``)
  which removes all guesswork about what a local model can do
* ``POST /api/chat`` — streaming NDJSON, with tool calls whose arguments are already objects
  and images passed as base64 (the ``/v1`` shim rejects image URLs and ignores ``tool_choice``)

Local models are always free, so they are the natural choice under FREE_ONLY and the only
choice in offline mode.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from core.errors import ProviderError, ProviderUnavailable
from core.logging_setup import get_logger
from providers.base import (
    ChatProvider,
    Message,
    ModelInfo,
    StreamChunk,
    ToolCall,
    ToolSpec,
    Usage,
)

log = get_logger("providers.ollama")

DEFAULT_BASE_URL = "http://127.0.0.1:11434"


class OllamaProvider(ChatProvider):
    name = "ollama"
    label = "Ollama (lokal)"
    is_local = True

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 300.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._capability_cache: dict[str, set[str]] = {}

    def set_base_url(self, base_url: str) -> None:
        if base_url.rstrip("/") != self.base_url:
            self.base_url = base_url.rstrip("/")
            self._capability_cache.clear()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    # --- interface ------------------------------------------------------------------

    async def is_available(self) -> tuple[bool, str]:
        try:
            client = await self._get_client()
            response = await client.get(f"{self.base_url}/api/tags", timeout=5.0)
        except httpx.HTTPError:
            return False, f"Ollama ist unter {self.base_url} nicht erreichbar"
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        try:
            count = len(response.json().get("models") or [])
        except ValueError:
            return False, "Unerwartete Antwort"
        if count == 0:
            return True, "Verbunden, aber kein Modell installiert (z. B. 'ollama pull qwen2.5')"
        return True, f"Verbunden, {count} Modell(e) installiert"

    async def list_models(self) -> list[ModelInfo]:
        client = await self._get_client()
        try:
            response = await client.get(f"{self.base_url}/api/tags", timeout=15.0)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Ollama is unreachable at {self.base_url}: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"Ollama returned HTTP {response.status_code}")
        try:
            entries = response.json().get("models") or []
        except ValueError as exc:
            raise ProviderError("Ollama model list is not valid JSON") from exc

        models: list[ModelInfo] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = str(entry.get("name") or entry.get("model") or "").strip()
            if not model_id:
                continue
            capabilities = await self._capabilities(model_id)
            details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
            size_gb = round(int(entry.get("size", 0)) / 1024**3, 1) if entry.get("size") else None
            description_parts = [p for p in (
                details.get("parameter_size"),
                details.get("quantization_level"),
                f"{size_gb} GB" if size_gb else None,
            ) if p]
            models.append(
                ModelInfo(
                    provider="ollama",
                    id=model_id,
                    name=model_id,
                    context_length=_context_from_details(details),
                    price_prompt="0",
                    price_completion="0",
                    is_free=True,           # a local model costs nothing to call
                    is_local=True,
                    supports_tools="tools" in capabilities if capabilities else None,
                    supports_vision="vision" in capabilities if capabilities else None,
                    # Ollama's structured-output support is a request-level feature ("format"),
                    # available for every model rather than advertised per model.
                    supports_structured=True,
                    supports_reasoning="thinking" in capabilities if capabilities else None,
                    description=" · ".join(description_parts),
                    raw=entry,
                )
            )
        log.info("Ollama: %d lokale Modelle gefunden", len(models))
        return models

    async def _capabilities(self, model_id: str) -> set[str]:
        """Ask ``/api/show`` what a model can do. Returns an empty set when unknown."""
        if model_id in self._capability_cache:
            return self._capability_cache[model_id]
        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/api/show", json={"model": model_id}, timeout=15.0
            )
            if response.status_code >= 400:
                return set()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return set()
        raw = payload.get("capabilities") if isinstance(payload, dict) else None
        capabilities = {str(c).lower() for c in raw} if isinstance(raw, list) else set()
        self._capability_cache[model_id] = capabilities
        return capabilities

    async def stream_chat(
        self,
        messages: Sequence[Message],
        model: str,
        *,
        tools: Sequence[ToolSpec] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens is not None:
            options["num_predict"] = max_tokens

        body: dict[str, Any] = {
            "model": model,
            "messages": _messages_to_ollama(messages),
            "stream": True,
        }
        if options:
            body["options"] = options
        if tools:
            body["tools"] = [tool.to_openai() for tool in tools]
        if response_format is not None:
            # Ollama takes a JSON Schema (or the literal "json") in "format".
            schema = response_format.get("json_schema", {}).get("schema") if isinstance(response_format, dict) else None
            body["format"] = schema or "json"
        if extra:
            body.update(extra)

        client = await self._get_client()
        usage = Usage()
        finish_reason: str | None = None
        emitted_tool_calls = 0

        try:
            async with client.stream(
                "POST", f"{self.base_url}/api/chat", json=body, timeout=self._timeout
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise ProviderError(
                        f"Ollama error {response.status_code}: {response.text[:200]}"
                    )
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue

                    if error := payload.get("error"):
                        yield StreamChunk(type="error", error=str(error))
                        return

                    message = payload.get("message") or {}
                    if content := message.get("content"):
                        yield StreamChunk(type="text", text=str(content))
                    if thinking := message.get("thinking"):
                        yield StreamChunk(type="reasoning", text=str(thinking))

                    for raw_call in message.get("tool_calls") or []:
                        function = (raw_call or {}).get("function") or {}
                        name = function.get("name")
                        if not name:
                            continue
                        arguments = function.get("arguments")
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError:
                                arguments = {"__malformed_arguments__": arguments}
                        if not isinstance(arguments, dict):
                            arguments = {"value": arguments}
                        emitted_tool_calls += 1
                        yield StreamChunk(
                            type="tool_call",
                            tool_call=ToolCall(
                                id=f"call_{emitted_tool_calls}", name=str(name), arguments=arguments
                            ),
                        )

                    if payload.get("done"):
                        usage = Usage(
                            prompt_tokens=payload.get("prompt_eval_count"),
                            completion_tokens=payload.get("eval_count"),
                            total_tokens=(payload.get("prompt_eval_count") or 0)
                            + (payload.get("eval_count") or 0),
                            cost_usd=0.0,   # local inference is free
                        )
                        finish_reason = str(payload.get("done_reason") or "stop")
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"Ollama stream failed: {exc}") from exc

        if finish_reason is None and emitted_tool_calls:
            finish_reason = "tool_calls"
        yield StreamChunk(type="usage", usage=usage)
        yield StreamChunk(type="done", finish_reason=finish_reason or "stop")


def _messages_to_ollama(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Ollama's native message format: images are a plain base64 list on the message."""
    payload: list[dict[str, Any]] = []
    for message in messages:
        entry: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.images:
            entry["images"] = [
                image.split(",", 1)[-1] if image.startswith("data:") else image
                for image in message.images
            ]
        if message.tool_calls:
            entry["tool_calls"] = [
                {"function": {"name": call.name, "arguments": call.arguments}}
                for call in message.tool_calls
            ]
        if message.role == "tool" and message.name:
            entry["tool_name"] = message.name
        payload.append(entry)
    return payload


def _context_from_details(details: dict[str, Any]) -> int | None:
    """Ollama's /api/tags does not report context length; report Unknown instead of guessing."""
    value = details.get("context_length") or details.get("num_ctx")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
