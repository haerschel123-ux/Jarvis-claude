"""Provider registry and persistent model catalogue (Spec §7, §8).

The catalogue is refreshed *from the providers*, never from a hardcoded list, and cached in
SQLite so JARVIS still knows what exists while offline or while a provider is down.

Filtering happens here so the UI, the router and the agents all apply the same rules — in
particular the FREE_ONLY rule, which must never be bypassed by accident (Spec §9).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from core.config import Settings, get_settings
from core.errors import JarvisError
from core.logging_setup import get_logger
from core.secrets import secret_store
from memory.database import Database, db
from providers.base import ChatProvider, ModelInfo
from providers.ollama import OllamaProvider
from providers.openai_compatible import OpenAICompatibleProvider
from providers.openrouter import FREE_ROUTER_ID, OpenRouterProvider

log = get_logger("catalog")


@dataclass(slots=True)
class ProviderStatus:
    name: str
    label: str
    enabled: bool
    available: bool
    reason: str
    is_local: bool
    model_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "enabled": self.enabled,
            "available": self.available,
            "reason": self.reason,
            "is_local": self.is_local,
            "model_count": self.model_count,
        }


class ModelCatalog:
    """Owns the provider instances and the cached catalogue."""

    def __init__(self, database: Database | None = None) -> None:
        self._db = database or db
        self._providers: dict[str, ChatProvider] = {}
        self._last_refresh: float = 0.0
        self._last_error: dict[str, str] = {}

    # --- providers ------------------------------------------------------------------

    def configure(self, settings: Settings | None = None) -> None:
        """(Re)build provider instances from the current settings and stored secrets."""
        settings = settings or get_settings()
        provider_settings = settings.providers

        if provider_settings.openrouter_enabled:
            existing = self._providers.get("openrouter")
            key = secret_store.get("openrouter_api_key")
            if isinstance(existing, OpenRouterProvider):
                existing.set_api_key(key)
                existing.base_url = provider_settings.openrouter_base_url.rstrip("/")
            else:
                self._providers["openrouter"] = OpenRouterProvider(
                    api_key=key,
                    base_url=provider_settings.openrouter_base_url,
                    referer=provider_settings.app_referer,
                    title=provider_settings.app_title,
                    timeout=settings.models.request_timeout_seconds,
                )
        else:
            self._providers.pop("openrouter", None)

        if provider_settings.ollama_enabled:
            existing = self._providers.get("ollama")
            if isinstance(existing, OllamaProvider):
                existing.set_base_url(provider_settings.ollama_base_url)
            else:
                self._providers["ollama"] = OllamaProvider(provider_settings.ollama_base_url)
        else:
            self._providers.pop("ollama", None)

        if provider_settings.custom_enabled and provider_settings.custom_base_url:
            existing = self._providers.get("custom")
            key = secret_store.get("custom_openai_api_key")
            if isinstance(existing, OpenAICompatibleProvider):
                existing.configure(
                    provider_settings.custom_base_url,
                    key,
                    label=provider_settings.custom_label,
                    treat_as_free=provider_settings.custom_is_free,
                    supports_tools=provider_settings.custom_supports_tools,
                    supports_vision=provider_settings.custom_supports_vision,
                )
                existing.is_local = provider_settings.custom_is_local
            else:
                self._providers["custom"] = OpenAICompatibleProvider(
                    provider_settings.custom_base_url,
                    key,
                    label=provider_settings.custom_label,
                    treat_as_free=provider_settings.custom_is_free,
                    treat_as_local=provider_settings.custom_is_local,
                    supports_tools=provider_settings.custom_supports_tools,
                    supports_vision=provider_settings.custom_supports_vision,
                )
        else:
            self._providers.pop("custom", None)

    def get_provider(self, name: str) -> ChatProvider | None:
        return self._providers.get(name)

    @property
    def providers(self) -> dict[str, ChatProvider]:
        return dict(self._providers)

    async def close(self) -> None:
        for provider in self._providers.values():
            await provider.close()

    async def provider_statuses(self) -> list[ProviderStatus]:
        settings = get_settings()
        statuses: list[ProviderStatus] = []
        for name, provider in self._providers.items():
            if settings.models.offline_mode and not provider.is_local:
                statuses.append(
                    ProviderStatus(name, provider.label, True, False,
                                   "Offline-Modus ist aktiv", provider.is_local,
                                   await self._count(name))
                )
                continue
            available, reason = await provider.is_available()
            statuses.append(
                ProviderStatus(name, provider.label, True, available, reason,
                               provider.is_local, await self._count(name))
            )
        return statuses

    async def _count(self, provider: str) -> int:
        value = await self._db.fetch_value(
            "SELECT COUNT(*) FROM model_catalog WHERE provider = ?", (provider,)
        )
        return int(value or 0)

    # --- refresh --------------------------------------------------------------------

    async def refresh(self, force: bool = False) -> dict[str, Any]:
        """Reload every reachable provider's catalogue into the database."""
        settings = get_settings()
        if not force and not await self.is_stale(settings):
            return {"refreshed": False, "reason": "Katalog ist aktuell",
                    "models": await self.count_all()}

        if not self._providers:
            # Configure lazily on first use. Rebuilding on every refresh would clobber
            # providers a caller installed deliberately.
            self.configure(settings)
        refreshed: dict[str, int] = {}
        errors: dict[str, str] = {}

        for name, provider in self._providers.items():
            if settings.models.offline_mode and not provider.is_local:
                continue
            try:
                models = await provider.list_models()
            except JarvisError as exc:
                errors[name] = exc.user_message or str(exc)
                log.warning("Katalog-Aktualisierung für %s fehlgeschlagen: %s", name, exc)
                continue
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
                log.exception("Unerwarteter Fehler beim Laden der Modelle von %s", name)
                continue
            await self._store(name, models)
            refreshed[name] = len(models)

        self._last_error = errors
        self._last_refresh = time.time()
        await self._db.kv_set("catalog_refreshed_at", self._last_refresh)
        total = await self.count_all()
        log.info("Modellkatalog aktualisiert: %s (gesamt %d)", refreshed or "nichts", total)
        return {"refreshed": True, "providers": refreshed, "errors": errors, "models": total}

    async def _store(self, provider: str, models: Iterable[ModelInfo]) -> None:
        """Replace this provider's rows. Other providers' entries stay untouched."""
        models = list(models)
        await self._db.execute("DELETE FROM model_catalog WHERE provider = ?", (provider,))
        if not models:
            return
        await self._db.execute_many(
            """
            INSERT INTO model_catalog
                (provider, model_id, name, context_length, price_prompt, price_completion,
                 is_free, supports_tools, supports_vision, supports_structured,
                 supports_reasoning, raw, refreshed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            [
                (
                    provider,
                    m.id,
                    m.name,
                    m.context_length,
                    m.price_prompt,
                    m.price_completion,
                    int(m.is_free),
                    _tri(m.supports_tools),
                    _tri(m.supports_vision),
                    _tri(m.supports_structured),
                    _tri(m.supports_reasoning),
                    json.dumps(
                        {"is_local": m.is_local, "is_router": m.is_router,
                         "description": m.description, "raw": m.raw},
                        ensure_ascii=False, default=str,
                    ),
                )
                for m in models
            ],
        )

    async def is_stale(self, settings: Settings | None = None) -> bool:
        settings = settings or get_settings()
        if await self.count_all() == 0:
            return True
        last = await self._db.kv_get("catalog_refreshed_at", 0) or 0
        max_age = max(settings.models.catalog_refresh_hours, 1) * 3600
        return (time.time() - float(last)) > max_age

    async def count_all(self) -> int:
        return int(await self._db.fetch_value("SELECT COUNT(*) FROM model_catalog") or 0)

    # --- queries --------------------------------------------------------------------

    async def all_models(self) -> list[ModelInfo]:
        rows = await self._db.fetch_all(
            "SELECT * FROM model_catalog ORDER BY is_free DESC, provider, model_id"
        )
        return [_row_to_model(row) for row in rows]

    async def get(self, model_key: str) -> ModelInfo | None:
        """Look a model up by ``provider:id`` or by bare id (first provider that has it)."""
        if ":" in model_key:
            provider, _, model_id = model_key.partition(":")
            row = await self._db.fetch_one(
                "SELECT * FROM model_catalog WHERE provider = ? AND model_id = ?",
                (provider, model_id),
            )
            if row:
                return _row_to_model(row)
        row = await self._db.fetch_one(
            "SELECT * FROM model_catalog WHERE model_id = ? ORDER BY is_free DESC LIMIT 1",
            (model_key,),
        )
        return _row_to_model(row) if row else None

    async def filter(
        self,
        *,
        free_only: bool = False,
        local_only: bool = False,
        provider: str | None = None,
        needs_tools: bool = False,
        needs_vision: bool = False,
        needs_structured: bool = False,
        needs_reasoning: bool = False,
        min_context: int | None = None,
        search: str | None = None,
        include_routers: bool = True,
    ) -> list[ModelInfo]:
        """Filter the catalogue.

        A capability requirement excludes models whose support is *Unknown*: when a task
        genuinely needs tool calling, picking a model that might not support it wastes a
        request and confuses the user (Spec §8 — do not guess).
        """
        models = await self.all_models()
        result: list[ModelInfo] = []
        needle = (search or "").strip().lower()

        for model in models:
            if free_only and not model.is_free:
                continue
            if local_only and not model.is_local:
                continue
            if provider and model.provider != provider:
                continue
            if not include_routers and model.is_router:
                continue
            if needs_tools and model.supports_tools is not True and not model.is_router:
                continue
            if needs_vision and model.supports_vision is not True and not model.is_router:
                continue
            if needs_structured and model.supports_structured is not True and not model.is_router:
                continue
            if needs_reasoning and model.supports_reasoning is not True:
                continue
            if min_context is not None and (model.context_length or 0) < min_context:
                continue
            if needle and needle not in f"{model.id} {model.name} {model.description}".lower():
                continue
            result.append(model)
        return result

    async def free_router(self) -> ModelInfo | None:
        """The configured free router, which is the last resort of the free fallback chain."""
        settings = get_settings()
        return await self.get(f"openrouter:{settings.models.free_router_model}") or await self.get(
            FREE_ROUTER_ID
        )

    @property
    def last_errors(self) -> dict[str, str]:
        return dict(self._last_error)


def _tri(value: bool | None) -> int | None:
    """Store a tri-state flag as 1 / 0 / NULL so Unknown survives the round trip."""
    return None if value is None else int(value)


def _from_tri(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _row_to_model(row: dict[str, Any]) -> ModelInfo:
    try:
        extra = json.loads(row.get("raw") or "{}")
    except json.JSONDecodeError:
        extra = {}
    return ModelInfo(
        provider=row["provider"],
        id=row["model_id"],
        name=row.get("name") or row["model_id"],
        context_length=row.get("context_length"),
        price_prompt=row.get("price_prompt"),
        price_completion=row.get("price_completion"),
        is_free=bool(row.get("is_free")),
        supports_tools=_from_tri(row.get("supports_tools")),
        supports_vision=_from_tri(row.get("supports_vision")),
        supports_structured=_from_tri(row.get("supports_structured")),
        supports_reasoning=_from_tri(row.get("supports_reasoning")),
        is_local=bool(extra.get("is_local")),
        is_router=bool(extra.get("is_router")),
        description=extra.get("description", ""),
        raw=extra.get("raw", {}),
    )


catalog = ModelCatalog()
