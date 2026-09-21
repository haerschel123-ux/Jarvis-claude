"""Model catalogue persistence and routing policy (Spec §7, §9, §11, §12)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from core.config import Settings
from core.enums import RouterMode
from core.errors import NoModelAvailable, PaidModelBlocked
from core.model_profiles import Requirements
from core.model_router import ModelRouter
from memory.database import Database
from providers.base import ModelInfo
from providers.catalog import ModelCatalog
from tests.fakes import ScriptedProvider, free_model, paid_model

FREE_CODER = free_model("free/coder", supports_tools=True, supports_vision=False, context_length=32768)
FREE_VISION = free_model("free/vision", supports_vision=True, supports_tools=True, context_length=200000)
FREE_TINY = free_model("free/tiny-mini", context_length=8192, supports_tools=False)
PAID_BIG = paid_model("paid/gpt-large", supports_vision=True, supports_reasoning=True)
LOCAL_MODEL = free_model("local/qwen2.5:7b", provider="local", is_local=True, supports_vision=False)
FREE_ROUTER = ModelInfo(provider="scripted", id="openrouter/free", name="Free Router",
                        is_free=True, is_router=True)

ALL_MODELS = [FREE_CODER, FREE_VISION, FREE_TINY, PAID_BIG, FREE_ROUTER]


@pytest.fixture
async def catalog(database: Database) -> AsyncIterator[ModelCatalog]:
    instance = ModelCatalog(database)
    instance._providers = {                                   # noqa: SLF001 - test wiring
        "scripted": ScriptedProvider(ALL_MODELS),
        "local": ScriptedProvider([LOCAL_MODEL], is_local=True),
    }
    await instance.refresh(force=True)
    yield instance


@pytest.fixture
def router(catalog: ModelCatalog) -> ModelRouter:
    return ModelRouter(catalog)


def settings_with(**overrides) -> Settings:
    settings = Settings()
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(settings, section), field, value)
    return settings


# --- catalogue ----------------------------------------------------------------------------


async def test_refresh_stores_every_provider(catalog: ModelCatalog) -> None:
    assert await catalog.count_all() == len(ALL_MODELS) + 1   # + the local provider's model
    assert {m.id for m in await catalog.all_models()} >= {m.id for m in ALL_MODELS}


async def test_tri_state_capabilities_survive_the_database(database: Database) -> None:
    """Unknown must stay Unknown after a round trip, not collapse to False (Spec §8)."""
    instance = ModelCatalog(database)
    unknown = ModelInfo(provider="scripted", id="mystery/x", supports_tools=None,
                        supports_vision=None, supports_structured=None, supports_reasoning=None)
    instance._providers = {"scripted": ScriptedProvider([unknown])}   # noqa: SLF001
    await instance.refresh(force=True)
    stored = await instance.get("scripted:mystery/x")
    assert stored is not None
    assert stored.supports_tools is None
    assert stored.to_dict()["display"]["tools"] == "Unknown"


async def test_refresh_replaces_only_its_own_provider(catalog: ModelCatalog, database: Database) -> None:
    catalog._providers["scripted"] = ScriptedProvider([FREE_CODER])   # noqa: SLF001
    await catalog.refresh(force=True)
    remaining = {m.key for m in await catalog.all_models()}
    assert "scripted:free/coder" in remaining
    assert "local:local/qwen2.5:7b" in remaining      # untouched by the other provider's refresh
    assert "scripted:paid/gpt-large" not in remaining


async def test_filter_free_only(catalog: ModelCatalog) -> None:
    free = await catalog.filter(free_only=True)
    assert all(m.is_free for m in free)
    assert "paid/gpt-large" not in {m.id for m in free}


async def test_capability_filter_excludes_unknown_support(database: Database) -> None:
    """Asking for tools must not return a model whose tool support is merely unknown."""
    instance = ModelCatalog(database)
    unknown = ModelInfo(provider="scripted", id="mystery/x", is_free=True, supports_tools=None)
    instance._providers = {"scripted": ScriptedProvider([unknown, FREE_CODER])}   # noqa: SLF001
    await instance.refresh(force=True)
    result = await instance.filter(needs_tools=True)
    assert {m.id for m in result} == {"free/coder"}


async def test_lookup_by_bare_id_and_by_provider_key(catalog: ModelCatalog) -> None:
    assert (await catalog.get("free/coder")).id == "free/coder"
    assert (await catalog.get("scripted:free/coder")).provider == "scripted"
    assert await catalog.get("does/not-exist") is None


# --- routing ------------------------------------------------------------------------------


async def test_auto_mode_picks_by_priority(router: ModelRouter) -> None:
    selection = await router.select(
        "vision", requirements=Requirements(needs_vision=True), settings=settings_with()
    )
    assert selection.model.id == "free/vision"
    assert selection.reason


async def test_free_only_never_selects_a_paid_model(router: ModelRouter) -> None:
    """Spec §9: with FREE_ONLY on, a paid model is never used without being asked."""
    for kind in ("chat", "coding", "vision", "research", "reasoning"):
        selection = await router.select(kind, settings=settings_with(**{"models.free_only": True}))
        assert selection.model.is_free, f"{kind} selected paid model {selection.model.id}"


async def test_explicitly_requesting_a_paid_model_is_blocked(router: ModelRouter) -> None:
    with pytest.raises(PaidModelBlocked) as info:
        await router.select(
            "chat", explicit_model="paid/gpt-large", settings=settings_with(**{"models.free_only": True})
        )
    assert "FREE ONLY" in info.value.user_message


async def test_paid_model_is_allowed_once_free_only_is_off(router: ModelRouter) -> None:
    selection = await router.select(
        "chat", explicit_model="paid/gpt-large", settings=settings_with(**{"models.free_only": False})
    )
    assert selection.model.id == "paid/gpt-large"


async def test_local_only_mode_stays_local(router: ModelRouter) -> None:
    selection = await router.select(
        "chat", settings=settings_with(**{"models.router_mode": RouterMode.LOCAL_ONLY})
    )
    assert selection.model.is_local is True


async def test_offline_mode_forces_local(router: ModelRouter) -> None:
    selection = await router.select("chat", settings=settings_with(**{"models.offline_mode": True}))
    assert selection.model.is_local is True


async def test_offline_mode_rejects_a_cloud_model_even_when_named(router: ModelRouter) -> None:
    with pytest.raises(NoModelAvailable):
        await router.select(
            "chat", explicit_model="free/coder", settings=settings_with(**{"models.offline_mode": True})
        )


async def test_hybrid_prefers_local_when_it_fits(router: ModelRouter) -> None:
    selection = await router.select(
        "chat", settings=settings_with(**{"models.router_mode": RouterMode.HYBRID})
    )
    assert selection.model.is_local is True
    assert "HYBRID" in selection.reason


async def test_hybrid_falls_back_to_cloud_when_local_cannot_do_it(router: ModelRouter) -> None:
    selection = await router.select(
        "vision",
        requirements=Requirements(needs_vision=True),
        settings=settings_with(**{"models.router_mode": RouterMode.HYBRID}),
    )
    assert selection.model.id == "free/vision"   # the local model has no vision support


async def test_manual_mode_uses_the_pinned_model(router: ModelRouter) -> None:
    settings = settings_with(**{"models.router_mode": RouterMode.MANUAL})
    settings.models.preferred_chat_model = "free/tiny-mini"
    selection = await router.select("chat", settings=settings)
    assert selection.model.id == "free/tiny-mini"


async def test_manual_mode_with_an_unknown_pin_falls_back_to_auto(router: ModelRouter) -> None:
    settings = settings_with(**{"models.router_mode": RouterMode.MANUAL})
    settings.models.preferred_chat_model = "ghost/model"
    selection = await router.select("chat", settings=settings)
    assert selection.model.id in {m.id for m in ALL_MODELS if m.is_free} | {"local/qwen2.5:7b"}


async def test_fallback_chain_ends_at_the_free_router(router: ModelRouter) -> None:
    """Preferred free model → another free model → free router (Spec §9)."""
    selection = await router.select(
        "chat", explicit_model="free/coder", settings=settings_with()
    )
    assert all(m.is_free for m in selection.fallbacks)
    assert selection.fallbacks[-1].is_router is True


async def test_unsatisfiable_requirement_lands_on_the_free_router(router: ModelRouter) -> None:
    """No free model confirms reasoning support, so the free router takes the request.

    The router filters for the needed feature itself, which is exactly why it is the
    documented fallback — and crucially it does so without touching a paid model.
    """
    selection = await router.select(
        "reasoning", requirements=Requirements(needs_reasoning=True),
        settings=settings_with(**{"models.free_only": True}),
    )
    assert selection.model.is_router is True
    assert selection.model.is_free is True


async def test_without_a_router_an_impossible_requirement_fails_loudly(database: Database) -> None:
    """No router to absorb the request and nothing fits: say so instead of picking anything."""
    instance = ModelCatalog(database)
    instance._providers = {"scripted": ScriptedProvider([FREE_CODER, FREE_TINY])}   # noqa: SLF001
    await instance.refresh(force=True)
    with pytest.raises(NoModelAvailable) as info:
        await ModelRouter(instance).select(
            "chat", requirements=Requirements(min_context=10_000_000),
            settings=settings_with(**{"models.free_only": True}),
        )
    assert "Anforderungen" in info.value.user_message


async def test_empty_catalogue_explains_what_to_do(database: Database) -> None:
    empty = ModelCatalog(database)
    empty._providers = {"scripted": ScriptedProvider([])}   # noqa: SLF001
    with pytest.raises(NoModelAvailable) as info:
        await ModelRouter(empty).select("chat", settings=settings_with())
    assert "OpenRouter" in info.value.user_message or "Ollama" in info.value.user_message


async def test_only_paid_models_available_blocks_with_an_explanation(database: Database) -> None:
    instance = ModelCatalog(database)
    instance._providers = {"scripted": ScriptedProvider([PAID_BIG])}   # noqa: SLF001
    await instance.refresh(force=True)
    with pytest.raises(PaidModelBlocked):
        await ModelRouter(instance).select("chat", settings=settings_with(**{"models.free_only": True}))


async def test_multi_model_team_gets_distinct_models(router: ModelRouter) -> None:
    """An independent review is pointless if the reviewer is the same model (Spec §32)."""
    team = await router.select_team(
        {"coding": Requirements(needs_tools=True), "review": Requirements()},
        settings=settings_with(),
    )
    assert set(team) == {"coding", "review"}
    assert team["coding"].model.key != team["review"].model.key


async def test_user_priorities_change_the_outcome(router: ModelRouter) -> None:
    settings = settings_with()
    settings.models.priorities["chat"] = ["long_context"]
    by_context = await router.select("chat", settings=settings)
    settings.models.priorities["chat"] = ["speed"]
    by_speed = await router.select("chat", settings=settings)
    assert by_context.model.id == "free/vision"      # 200k context
    assert by_speed.model.id != by_context.model.id
