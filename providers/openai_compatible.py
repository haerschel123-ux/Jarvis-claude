"""Generic OpenAI-compatible provider (Spec §10).

Covers LM Studio, llama.cpp's server, vLLM, Groq, Mistral, and anything else that exposes
``/models`` and ``/chat/completions`` in the OpenAI shape. Because such a server publishes no
capability metadata, every capability flag is reported as *Unknown* rather than assumed, and
pricing is left to the user's own configuration.
"""

from __future__ import annotations

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
    ToolSpec,
    messages_to_openai,
)
from providers.openai_protocol import StreamState, iter_sse_payloads

log = get_logger("providers.custom")


class OpenAICompatibleProvider(ChatProvider):
    name = "custom"
    is_local = False

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        label: str = "Custom API",
        treat_as_free: bool = False,
        treat_as_local: bool = False,
        timeout: float = 180.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.label = label
        self._api_key = api_key
        self._treat_as_free = treat_as_free
        self.is_local = treat_as_local
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    def configure(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        label: str | None = None,
        treat_as_free: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        if label is not None:
            self.label = label
        if treat_as_free is not None:
            self._treat_as_free = treat_as_free

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def is_available(self) -> tuple[bool, str]:
        if not self.base_url:
            return False, "Keine Basis-URL konfiguriert"
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=8.0
            )
        except httpx.HTTPError:
            return False, f"{self.base_url} ist nicht erreichbar"
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        return True, "Verbunden"

    async def list_models(self) -> list[ModelInfo]:
        if not self.base_url:
            return []
        client = await self._get_client()
        try:
            response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.label} is unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise ProviderError(f"{self.label} returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"{self.label} returned a response that is not JSON") from exc

        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return []

        models: list[ModelInfo] = []
        for entry in entries:
            model_id = entry.get("id") if isinstance(entry, dict) else entry
            if not model_id:
                continue
            models.append(
                ModelInfo(
                    provider=self.name,
                    id=str(model_id),
                    name=str(model_id),
                    # A plain OpenAI-compatible server publishes no capability metadata, so
                    # everything stays Unknown instead of being assumed (Spec §8).
                    context_length=None,
                    price_prompt="0" if self._treat_as_free else None,
                    price_completion="0" if self._treat_as_free else None,
                    is_free=self._treat_as_free,
                    is_local=self.is_local,
                    supports_tools=None,
                    supports_vision=None,
                    supports_structured=None,
                    supports_reasoning=None,
                    description=self.label,
                    raw=entry if isinstance(entry, dict) else {"id": model_id},
                )
            )
        log.info("%s: %d Modelle geladen", self.label, len(models))
        return models

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
        body: dict[str, Any] = {
            "model": model,
            "messages": messages_to_openai(messages),
            "stream": True,
        }
        if tools:
            body["tools"] = [tool.to_openai() for tool in tools]
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if response_format is not None:
            body["response_format"] = response_format
        if extra:
            body.update(extra)

        client = await self._get_client()
        state = StreamState()
        try:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
                timeout=self._timeout,
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise ProviderError(
                        f"{self.label} error {response.status_code}: {response.text[:200]}"
                    )
                async for payload in iter_sse_payloads(response.aiter_lines()):
                    for chunk in state.ingest(payload):
                        yield chunk
                        if chunk.type == "error":
                            return
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{self.label} stream failed: {exc}") from exc

        for chunk in state.finalise():
            yield chunk
