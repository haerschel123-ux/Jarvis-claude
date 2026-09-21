"""Model router (Spec §9, §11).

Decides *which* model answers a given request, under the user's policy:

``AUTO``        rank by capability against the task's priority list
``MANUAL``      use the model the user pinned for this task kind
``FREE_ONLY``   as AUTO, but paid models are not candidates at all
``LOCAL_ONLY``  only models served by Ollama or a local endpoint
``HYBRID``      prefer local, fall back to the cloud
``MULTI_MODEL`` as AUTO, but also hands out distinct models for planner/executor/reviewer

Two rules are absolute:

* With FREE_ONLY on, a paid model is **never** used without being asked (Spec §9). If nothing
  free fits, the router raises :class:`PaidModelBlocked` instead of quietly spending money.
* In offline mode, only local providers are candidates (Spec §84).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import Settings, get_settings
from core.enums import RouterMode
from core.errors import NoModelAvailable, PaidModelBlocked
from core.logging_setup import get_logger
from core.model_profiles import Requirements, Score, rank
from providers.base import ChatProvider, ModelInfo
from providers.catalog import ModelCatalog, catalog

log = get_logger("router")

# Task kinds and the capability priority list used when the user has not overridden it.
DEFAULT_PRIORITIES: dict[str, list[str]] = {
    "chat": ["chat", "speed", "free"],
    "voice_chat": ["speed", "free", "chat"],
    "coding": ["coding", "tools", "long_context", "speed"],
    "vision": ["vision", "free", "speed"],
    "research": ["tools", "long_context", "reasoning"],
    "reasoning": ["reasoning", "long_context", "coding"],
    "planning": ["reasoning", "tools", "long_context"],
    "review": ["reasoning", "coding", "long_context"],
    "classification": ["speed", "structured_output", "free"],
}


@dataclass(slots=True)
class ModelSelection:
    """The chosen model plus everything needed to explain and retry the choice."""

    model: ModelInfo
    provider: ChatProvider
    reason: str
    task_kind: str
    fallbacks: list[ModelInfo] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.to_dict(),
            "provider": self.provider.name,
            "reason": self.reason,
            "task_kind": self.task_kind,
            "fallbacks": [m.key for m in self.fallbacks],
            "why": [s.to_dict() for s in self.scores[:3]],
        }


class ModelRouter:
    def __init__(self, model_catalog: ModelCatalog | None = None) -> None:
        self._catalog = model_catalog or catalog

    # --- policy ---------------------------------------------------------------------

    def _effective_policy(self, settings: Settings) -> tuple[bool, bool]:
        """Return ``(free_only, local_only)`` after combining every relevant setting."""
        mode = settings.models.router_mode
        free_only = settings.models.free_only or mode is RouterMode.FREE_ONLY
        local_only = mode is RouterMode.LOCAL_ONLY or settings.models.offline_mode
        return free_only, local_only

    def priorities_for(self, task_kind: str, settings: Settings) -> list[str]:
        configured = settings.models.priorities.get(task_kind)
        if configured:
            return list(configured)
        return list(DEFAULT_PRIORITIES.get(task_kind, DEFAULT_PRIORITIES["chat"]))

    def _pinned_model(self, task_kind: str, settings: Settings) -> str | None:
        return {
            "chat": settings.models.preferred_chat_model,
            "voice_chat": settings.models.preferred_fast_model or settings.models.preferred_chat_model,
            "coding": settings.models.preferred_coding_model,
            "vision": settings.models.preferred_vision_model,
            "classification": settings.models.preferred_fast_model,
        }.get(task_kind)

    # --- selection ------------------------------------------------------------------

    async def select(
        self,
        task_kind: str = "chat",
        *,
        requirements: Requirements | None = None,
        explicit_model: str | None = None,
        settings: Settings | None = None,
    ) -> ModelSelection:
        settings = settings or get_settings()
        requirements = requirements or Requirements()
        free_only, local_only = self._effective_policy(settings)
        mode = settings.models.router_mode

        # 1. An explicitly requested model wins, but still has to pass the cost policy.
        if explicit_model:
            selection = await self._select_explicit(
                explicit_model, task_kind, free_only, local_only, settings
            )
            if selection is not None:
                return selection

        # 2. In MANUAL mode the user's pinned model is authoritative.
        if mode is RouterMode.MANUAL:
            pinned = self._pinned_model(task_kind, settings)
            if pinned:
                selection = await self._select_explicit(
                    pinned, task_kind, free_only, local_only, settings
                )
                if selection is not None:
                    return selection
            log.info("MANUAL-Modus ohne verwendbares festgelegtes Modell — wähle automatisch")

        candidates = await self._candidates(free_only, local_only, settings)
        if not candidates:
            raise await self._explain_empty(free_only, local_only, settings)

        priorities = self.priorities_for(task_kind, settings)

        # 3. HYBRID prefers a local model when one actually fits the requirements.
        if mode is RouterMode.HYBRID:
            local = [m for m in candidates if m.is_local]
            ranked_local = rank(local, priorities, requirements)
            if ranked_local:
                return self._build(ranked_local, task_kind, "Lokales Modell bevorzugt (HYBRID)")

        ranked = rank(candidates, priorities, requirements)
        if not ranked:
            return await self._router_fallback(task_kind, requirements, free_only, local_only, settings)

        reason = self._reason_for(mode, free_only, local_only, priorities)
        return self._build(ranked, task_kind, reason)

    async def _select_explicit(
        self,
        model_key: str,
        task_kind: str,
        free_only: bool,
        local_only: bool,
        settings: Settings,
    ) -> ModelSelection | None:
        model = await self._catalog.get(model_key)
        if model is None:
            log.warning("Modell '%s' ist nicht im Katalog", model_key)
            return None
        if free_only and not model.is_free:
            raise PaidModelBlocked(
                f"Model {model.key} is paid while FREE_ONLY is enabled",
                detail={"model": model.key},
                user_message=(
                    f"'{model.name}' ist kostenpflichtig. FREE ONLY ist aktiv, deshalb habe ich "
                    "es nicht verwendet. Du kannst kostenpflichtige Modelle in den "
                    "Einstellungen freigeben."
                ),
            )
        if local_only and not model.is_local:
            raise NoModelAvailable(
                f"Model {model.key} is not local while local-only is enforced",
                user_message=(
                    f"'{model.name}' läuft nicht lokal. Im Offline- bzw. Nur-lokal-Modus kann "
                    "ich es nicht verwenden."
                ),
            )
        provider = self._catalog.get_provider(model.provider)
        if provider is None:
            log.warning("Anbieter '%s' ist nicht konfiguriert", model.provider)
            return None
        fallbacks = await self._fallback_models(model, free_only, local_only, settings)
        return ModelSelection(
            model=model,
            provider=provider,
            reason="Ausdrücklich gewählt",
            task_kind=task_kind,
            fallbacks=fallbacks,
        )

    async def _candidates(
        self, free_only: bool, local_only: bool, settings: Settings
    ) -> list[ModelInfo]:
        models = await self._catalog.filter(free_only=free_only, local_only=local_only)
        configured = set(self._catalog.providers)
        # A cached model whose provider is currently switched off is not a candidate.
        return [m for m in models if m.provider in configured]

    def _build(self, ranked: list[Score], task_kind: str, reason: str) -> ModelSelection:
        best = ranked[0]
        provider = self._catalog.get_provider(best.model.provider)
        if provider is None:  # pragma: no cover - guarded by _candidates
            raise NoModelAvailable(f"Provider {best.model.provider} is not configured")
        return ModelSelection(
            model=best.model,
            provider=provider,
            reason=reason,
            task_kind=task_kind,
            fallbacks=[s.model for s in ranked[1:4]],
            scores=ranked[:5],
        )

    async def _router_fallback(
        self,
        task_kind: str,
        requirements: Requirements,
        free_only: bool,
        local_only: bool,
        settings: Settings,
    ) -> ModelSelection:
        """Nothing satisfied the hard requirements — try the free router, then give up."""
        if not local_only:
            router_model = await self._catalog.free_router()
            provider = self._catalog.get_provider(router_model.provider) if router_model else None
            if router_model and provider is not None:
                log.info("Kein Modell erfüllt %s — nutze den Free Router", requirements.to_dict())
                return ModelSelection(
                    model=router_model,
                    provider=provider,
                    reason="Kein Modell erfüllte die Anforderungen — Free Router als Rückfallebene",
                    task_kind=task_kind,
                )
        missing = [key for key, value in requirements.to_dict().items() if value]
        raise NoModelAvailable(
            f"No model satisfies {missing}",
            detail=requirements.to_dict(),
            user_message=(
                "Kein verfügbares Modell erfüllt die Anforderungen dieser Aufgabe ("
                + ", ".join(missing or ["keine"])
                + "). Aktualisiere den Modellkatalog oder erlaube weitere Anbieter."
            ),
        )

    async def _fallback_models(
        self, chosen: ModelInfo, free_only: bool, local_only: bool, settings: Settings
    ) -> list[ModelInfo]:
        """Preferred free model → another free model → free router (Spec §9)."""
        others = [
            m for m in await self._candidates(free_only, local_only, settings)
            if m.key != chosen.key
        ]
        others.sort(key=lambda m: (not m.is_free, m.is_router, m.id))
        chain = others[:3]
        if not local_only:
            router_model = await self._catalog.free_router()
            if router_model and router_model.key not in {m.key for m in chain} | {chosen.key}:
                chain.append(router_model)
        return chain

    async def _explain_empty(
        self, free_only: bool, local_only: bool, settings: Settings
    ) -> Exception:
        """Turn "no candidates" into an actionable message rather than a bare failure."""
        total = await self._catalog.count_all()
        if total == 0:
            return NoModelAvailable(
                "Model catalogue is empty",
                user_message=(
                    "Der Modellkatalog ist leer. Hinterlege einen OpenRouter-Schlüssel oder "
                    "starte Ollama und aktualisiere dann die Modellliste."
                ),
            )
        if local_only:
            return NoModelAvailable(
                "No local models installed",
                user_message=(
                    "Es ist kein lokales Modell installiert. Installiere eines über Ollama "
                    "(z. B. 'ollama pull qwen2.5') oder deaktiviere den Offline-Modus."
                ),
            )
        if free_only:
            return PaidModelBlocked(
                "Only paid models are available while FREE_ONLY is enabled",
                user_message=(
                    "Es sind nur kostenpflichtige Modelle verfügbar. FREE ONLY ist aktiv, "
                    "deshalb habe ich keines gestartet. Du kannst kostenpflichtige Modelle "
                    "in den Einstellungen freigeben."
                ),
            )
        return NoModelAvailable(
            "No usable model",
            user_message="Es ist kein verwendbares Modell konfiguriert.",
        )

    @staticmethod
    def _reason_for(
        mode: RouterMode, free_only: bool, local_only: bool, priorities: list[str]
    ) -> str:
        parts = [f"Modus {mode.value}"]
        if free_only:
            parts.append("nur kostenlose Modelle")
        if local_only:
            parts.append("nur lokale Modelle")
        parts.append("Priorität: " + " > ".join(priorities[:3]))
        return ", ".join(parts)

    # --- multi-model ----------------------------------------------------------------

    async def select_team(
        self,
        roles: dict[str, Requirements],
        *,
        settings: Settings | None = None,
        distinct: bool = True,
    ) -> dict[str, ModelSelection]:
        """Pick a model per role for a multi-model workflow (Spec §11 MULTI_MODEL, §31).

        With ``distinct`` the router avoids handing the same model to, say, both the coder
        and the reviewer — an independent second opinion is the entire point (Spec §32). If
        not enough distinct models exist, roles share one rather than failing.
        """
        settings = settings or get_settings()
        used: set[str] = set()
        team: dict[str, ModelSelection] = {}
        for role, requirements in roles.items():
            selection = await self.select(role, requirements=requirements, settings=settings)
            if distinct and selection.model.key in used:
                alternative = next(
                    (m for m in selection.fallbacks if m.key not in used), None
                )
                if alternative is not None:
                    provider = self._catalog.get_provider(alternative.provider)
                    if provider is not None:
                        selection = ModelSelection(
                            model=alternative,
                            provider=provider,
                            reason=f"{selection.reason} (abweichend für unabhängige Prüfung)",
                            task_kind=role,
                            fallbacks=selection.fallbacks,
                        )
            used.add(selection.model.key)
            team[role] = selection
        return team


router = ModelRouter()
