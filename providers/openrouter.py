"""OpenRouter provider (Spec §8, §9).

Verified against the official API reference (see ``docs/VERIFIED_APIS.md``):

* ``GET  {base}/models`` returns ``{"data": [ ... ]}`` where each entry carries ``id``,
  ``name``, ``context_length``, ``pricing`` (string values, USD per token), ``architecture``
  (``input_modalities``) and ``supported_parameters``.
* ``POST {base}/chat/completions`` speaks the OpenAI format and streams SSE.
* ``openrouter/free`` is the Free Models Router: it picks a free model at random and filters
  by the capabilities a request needs. ``openrouter/auto`` also exists but is **not** free,
  so it is never treated as one.

Parsing is deliberately defensive: a field the provider does not send becomes ``None``
(rendered as *Unknown*), never a guess.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from core.errors import ConfigurationError, ProviderError, ProviderUnavailable, RateLimited
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

log = get_logger("providers.openrouter")

FREE_ROUTER_ID = "openrouter/free"
# openrouter/auto routes across paid models; it must never satisfy a FREE_ONLY policy.
PAID_ROUTER_IDS = frozenset({"openrouter/auto"})
ROUTER_IDS = frozenset({FREE_ROUTER_ID}) | PAID_ROUTER_IDS


def _price_is_zero(value: str | None) -> bool | None:
    """True/False when the price is known, None when the provider did not say."""
    if value is None:
        return None
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return None


def parse_model(raw: dict[str, Any]) -> ModelInfo:
    """Map one ``/models`` entry onto :class:`ModelInfo` without inventing anything."""
    model_id = str(raw.get("id") or "").strip()
    pricing = raw.get("pricing") if isinstance(raw.get("pricing"), dict) else {}
    price_prompt = pricing.get("prompt")
    price_completion = pricing.get("completion")
    price_prompt = str(price_prompt) if price_prompt is not None else None
    price_completion = str(price_completion) if price_completion is not None else None

    prompt_free = _price_is_zero(price_prompt)
    completion_free = _price_is_zero(price_completion)
    if prompt_free is not None and completion_free is not None:
        # Pricing is authoritative. A ":free" suffix on a priced model does not make it free.
        is_free = prompt_free and completion_free
    else:
        # Pricing unknown: fall back to the naming convention, which is the only signal left.
        is_free = model_id.endswith(":free") or model_id == FREE_ROUTER_ID
    if model_id in PAID_ROUTER_IDS:
        is_free = False

    supported = raw.get("supported_parameters")
    if isinstance(supported, list):
        params = {str(p).lower() for p in supported}
        supports_tools = bool(params & {"tools", "tool_choice"})
        supports_structured = bool(params & {"response_format", "structured_outputs"})
        supports_reasoning = bool(params & {"reasoning", "include_reasoning"})
    else:
        supports_tools = supports_structured = supports_reasoning = None

    architecture = raw.get("architecture") if isinstance(raw.get("architecture"), dict) else {}
    modalities = architecture.get("input_modalities")
    if isinstance(modalities, list):
        supports_vision = "image" in {str(m).lower() for m in modalities}
    elif isinstance(architecture.get("modality"), str):
        supports_vision = "image" in architecture["modality"].lower()
    else:
        supports_vision = None

    context_length = raw.get("context_length")
    if context_length is None and isinstance(raw.get("top_provider"), dict):
        context_length = raw["top_provider"].get("context_length")
    try:
        context_length = int(context_length) if context_length is not None else None
    except (TypeError, ValueError):
        context_length = None

    return ModelInfo(
        provider="openrouter",
        id=model_id,
        name=str(raw.get("name") or model_id),
        context_length=context_length,
        price_prompt=price_prompt,
        price_completion=price_completion,
        is_free=is_free,
        supports_tools=supports_tools,
        supports_vision=supports_vision,
        supports_structured=supports_structured,
        supports_reasoning=supports_reasoning,
        is_router=model_id in ROUTER_IDS,
        description=str(raw.get("description") or "")[:500],
        raw=raw,
    )


class OpenRouterProvider(ChatProvider):
    name = "openrouter"
    label = "OpenRouter"
    is_local = False

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        *,
        referer: str = "",
        title: str = "JARVIS",
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._referer = referer
        self._title = title
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    # --- plumbing -------------------------------------------------------------------

    def set_api_key(self, api_key: str | None) -> None:
        self._api_key = api_key

    @property
    def has_key(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ConfigurationError(
                "OpenRouter API key is not configured",
                user_message="Für OpenRouter fehlt noch der API-Schlüssel. "
                "Du findest ihn unter https://openrouter.ai/keys.",
            )
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        # Optional attribution headers used by OpenRouter's app ranking.
        if self._referer:
            headers["HTTP-Referer"] = self._referer
        if self._title:
            headers["X-Title"] = self._title
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

    # --- interface ------------------------------------------------------------------

    async def is_available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "Kein API-Schlüssel hinterlegt"
        try:
            client = await self._get_client()
            response = await client.get(
                f"{self.base_url}/models", headers=self._headers(), timeout=10.0
            )
        except httpx.HTTPError as exc:
            return False, f"Nicht erreichbar: {type(exc).__name__}"
        if response.status_code == 401:
            return False, "API-Schlüssel wurde abgelehnt"
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}"
        return True, "Verbunden"

    async def list_models(self) -> list[ModelInfo]:
        client = await self._get_client()
        try:
            response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"OpenRouter is unreachable: {exc}") from exc
        self._raise_for_status(response)

        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError("OpenRouter returned a response that is not JSON") from exc

        entries = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            raise ProviderError("OpenRouter model list has an unexpected shape")

        models = [parse_model(entry) for entry in entries if isinstance(entry, dict) and entry.get("id")]
        if not any(m.id == FREE_ROUTER_ID for m in models):
            # The free router is documented but is not always listed in /models; it must stay
            # selectable because it is the default cloud fallback (Spec §9).
            models.append(
                ModelInfo(
                    provider="openrouter",
                    id=FREE_ROUTER_ID,
                    name="OpenRouter Free Router",
                    is_free=True,
                    is_router=True,
                    description=(
                        "Wählt automatisch ein kostenloses Modell und filtert dabei nach den "
                        "benötigten Fähigkeiten (Bilder, Tools, strukturierte Ausgaben)."
                    ),
                )
            )
        log.info("OpenRouter: %d Modelle geladen (%d kostenlos)",
                 len(models), sum(1 for m in models if m.is_free))
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
                    self._raise_for_status(response)
                async for payload in iter_sse_payloads(response.aiter_lines()):
                    for chunk in state.ingest(payload):
                        yield chunk
                        if chunk.type == "error":
                            return
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"OpenRouter stream failed: {exc}") from exc

        for chunk in state.finalise():
            yield chunk

    # --- errors ---------------------------------------------------------------------

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = self._error_detail(response)
        if response.status_code in (401, 403):
            raise ConfigurationError(
                f"OpenRouter rejected the API key ({response.status_code}): {detail}",
                user_message="Der OpenRouter-Schlüssel wurde abgelehnt. Bitte prüfe ihn in den Einstellungen.",
            )
        if response.status_code == 429:
            retry_after = response.headers.get("retry-after")
            raise RateLimited(
                f"OpenRouter rate limit reached: {detail}",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if response.status_code == 402:
            raise ProviderError(
                f"OpenRouter reports insufficient credit: {detail}",
                user_message="OpenRouter meldet zu wenig Guthaben für dieses Modell.",
            )
        if response.status_code >= 500:
            raise ProviderUnavailable(f"OpenRouter server error {response.status_code}: {detail}")
        raise ProviderError(f"OpenRouter error {response.status_code}: {detail}")

    @staticmethod
    def _error_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return response.text[:200]
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                return str(error.get("message") or error)[:300]
            if error:
                return str(error)[:300]
        return str(payload)[:200]
