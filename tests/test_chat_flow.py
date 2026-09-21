"""End-to-end turn: intent → routing → streaming → persistence (Spec §13, §77, §107)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.assistant import AssistantCore, ToolOutcome, TurnContext
from core.config import Settings
from core.enums import Intent
from core.errors import ProviderUnavailable
from memory.conversations import ConversationStore
from memory.database import Database
from providers.base import ToolSpec
from providers.catalog import ModelCatalog
from tests.fakes import ScriptedProvider, free_model, text_turn, tool_turn

CHAT_MODEL = free_model("free/chat-mini", supports_tools=True, context_length=32768)
BACKUP_MODEL = free_model("free/backup", supports_tools=True, context_length=16384)


@pytest.fixture
async def wired(database: Database):
    """An assistant core wired to a scripted provider and an isolated database."""
    from core.model_router import ModelRouter

    provider = ScriptedProvider([CHAT_MODEL, BACKUP_MODEL])
    model_catalog = ModelCatalog(database)
    model_catalog._providers = {"scripted": provider}       # noqa: SLF001 - test wiring
    await model_catalog.refresh(force=True)

    store = ConversationStore(database)
    core = AssistantCore(model_router=ModelRouter(model_catalog), conversation_store=store)

    # The assistant resolves fallbacks through the global catalogue, so point it at ours.
    import providers.catalog as catalog_module

    original = catalog_module.catalog
    catalog_module.catalog = model_catalog
    try:
        yield core, provider, store, model_catalog
    finally:
        catalog_module.catalog = original


async def collect(core: AssistantCore, message: str, **kwargs) -> tuple[str, list]:
    events = [event async for event in core.run_turn(message, **kwargs)]
    text = "".join(e.data.get("text", "") for e in events if e.type == "delta")
    return text, events


# --- core pipeline ------------------------------------------------------------------------


async def test_simple_turn_streams_and_persists(wired) -> None:
    core, provider, store, _ = wired
    provider._script = [text_turn("Guten Abend. Womit kann ich helfen?")]   # noqa: SLF001

    text, events = await collect(core, "Hallo Jarvis")
    assert text == "Guten Abend. Womit kann ich helfen?"

    types = [e.type for e in events]
    assert types[0] == "turn.started"
    assert "model.selected" in types
    assert "context.built" in types
    assert types[-1] == "turn.completed"

    conversation_id = events[0].data["conversation_id"]
    messages = await store.messages(conversation_id)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == text
    assert messages[1]["model_id"] == CHAT_MODEL.key


async def test_intent_is_classified_and_reported(wired) -> None:
    core, provider, _, _ = wired
    provider._script = [text_turn("ok")]                                    # noqa: SLF001
    _, events = await collect(core, "Jarvis, prüfe meinen DayZ Server.")
    started = next(e for e in events if e.type == "turn.started")
    assert started.data["intent"]["intent"] == Intent.DAYZ_ACTION.value


async def test_first_message_titles_the_conversation(wired) -> None:
    core, provider, store, _ = wired
    provider._script = [text_turn("ok")]                                    # noqa: SLF001
    _, events = await collect(core, "Wie prüfe ich meine events.xml auf Fehler?")
    titled = next(e for e in events if e.type == "conversation.titled")
    assert "events.xml" in titled.data["title"]


async def test_history_is_reused_across_turns(wired) -> None:
    core, provider, store, _ = wired
    provider._script = [text_turn("Erste Antwort"), text_turn("Zweite Antwort")]   # noqa: SLF001

    _, events = await collect(core, "Erste Frage")
    conversation_id = events[0].data["conversation_id"]
    await collect(core, "Zweite Frage", conversation_id=conversation_id)

    second_call = provider.calls[1]
    roles = [m.role for m in second_call["messages"]]
    assert roles.count("user") >= 2          # the earlier turn is in the context
    assert roles[0] == "system"
    contents = " ".join(m.content for m in second_call["messages"])
    assert "Erste Frage" in contents and "Erste Antwort" in contents


async def test_system_prompt_states_platform_limits(wired) -> None:
    """Spec §108: the model must be told what this machine cannot do."""
    core, provider, _, _ = wired
    provider._script = [text_turn("ok")]                                    # noqa: SLF001
    await collect(core, "Hallo")
    system_message = provider.calls[0]["messages"][0]
    assert system_message.role == "system"
    assert "NICHT verfügbar" in system_message.content
    assert "Erfinde niemals" in system_message.content


# --- tools --------------------------------------------------------------------------------


class RecordingTools:
    """Minimal tool executor: records calls and returns a fixed result."""

    def __init__(self, result: str = "Discord wurde gestartet.") -> None:
        self.result = result
        self.calls: list = []

    def specs_for(self, intent: Intent, settings: Settings) -> list[ToolSpec]:
        return [ToolSpec("launch_app", "Startet ein Programm",
                         {"type": "object", "properties": {"app": {"type": "string"}}})]

    async def execute(self, call, *, context: TurnContext) -> ToolOutcome:
        self.calls.append(call)
        return ToolOutcome(call.id, call.name, True, self.result, {"app": call.arguments.get("app")})


async def test_tool_call_round_trip(wired) -> None:
    core, provider, store, _ = wired
    tools = RecordingTools()
    core.set_tool_executor(tools)
    provider._script = [                                                    # noqa: SLF001
        tool_turn("launch_app", {"app": "discord"}),
        text_turn("Discord ist geöffnet."),
    ]

    text, events = await collect(core, "Jarvis, öffne Discord.")
    assert text == "Discord ist geöffnet."
    assert [c.name for c in tools.calls] == ["launch_app"]
    assert tools.calls[0].arguments == {"app": "discord"}

    types = [e.type for e in events]
    assert "tool.requested" in types and "tool.result" in types

    # The tool result must reach the model as a tool message, not as a user instruction.
    second_call = provider.calls[1]
    tool_messages = [m for m in second_call["messages"] if m.role == "tool"]
    assert tool_messages and tool_messages[0].trust == "tool_result"


async def test_tools_are_not_offered_to_a_model_without_support(wired) -> None:
    """A model the user pinned that cannot call tools must not be handed any.

    Automatic routing would never pick such a model for a tool task, so this only happens
    when the user chooses one explicitly — and then the request must still work.
    """
    core, provider, _, model_catalog = wired
    no_tools = free_model("free/no-tools", supports_tools=False, context_length=32768)
    scripted = ScriptedProvider([CHAT_MODEL, no_tools], script=[text_turn("Das kann ich hier nicht.")])
    model_catalog._providers = {"scripted": scripted}                       # noqa: SLF001
    await model_catalog.refresh(force=True)
    core.set_tool_executor(RecordingTools())

    await collect(core, "Jarvis, öffne Discord.", explicit_model="free/no-tools")
    assert scripted.calls[0]["model"] == "free/no-tools"
    assert scripted.calls[0]["tools"] == []


async def test_explicit_model_choice_is_honoured(wired) -> None:
    """A model picked in the UI must actually be used, not quietly overridden."""
    core, provider, _, _ = wired
    provider._script = [text_turn("ok")]                                    # noqa: SLF001
    _, events = await collect(core, "Hallo", explicit_model="free/backup")
    selected = next(e for e in events if e.type == "model.selected")
    assert selected.data["model"]["id"] == "free/backup"
    assert provider.calls[0]["model"] == "free/backup"


async def test_tool_request_without_an_executor_is_admitted_not_faked(wired) -> None:
    """Spec §80: never claim an action happened when no tool ran."""
    core, provider, _, _ = wired
    core.set_tool_executor(None)
    provider._script = [tool_turn("launch_app", {"app": "discord"})]        # noqa: SLF001
    text, _ = await collect(core, "Jarvis, öffne Discord.")
    assert "keines verfügbar" in text


async def test_tool_loop_stops_at_the_limit(wired) -> None:
    """A model that keeps calling tools must not spin forever (Spec §101)."""
    core, provider, _, _ = wired
    core.set_tool_executor(RecordingTools())
    provider._script = [tool_turn("launch_app", {"app": "discord"})] * 40   # noqa: SLF001
    text, events = await collect(core, "Jarvis, öffne Discord.")
    assert "abgebrochen" in text
    assert len([e for e in events if e.type == "tool.requested"]) <= 8


# --- resilience ---------------------------------------------------------------------------


async def test_failing_model_falls_back_to_the_next(wired) -> None:
    core, provider, store, model_catalog = wired

    class FlakyProvider(ScriptedProvider):
        async def stream_chat(self, messages, model, **kwargs):
            self.calls.append({"model": model, "messages": list(messages), "tools": [], **kwargs})
            if model == CHAT_MODEL.id:
                raise ProviderUnavailable("primary is down")
            for chunk in text_turn("Antwort vom Rückfallmodell"):
                yield chunk

    flaky = FlakyProvider([CHAT_MODEL, BACKUP_MODEL])
    model_catalog._providers = {"scripted": flaky}                          # noqa: SLF001
    await model_catalog.refresh(force=True)

    text, _ = await collect(core, "Hallo")
    assert text == "Antwort vom Rückfallmodell"
    assert [c["model"] for c in flaky.calls] == [CHAT_MODEL.id, BACKUP_MODEL.id]


async def test_all_models_failing_reports_an_error_turn(wired) -> None:
    core, provider, store, model_catalog = wired

    class DeadProvider(ScriptedProvider):
        async def stream_chat(self, messages, model, **kwargs):
            raise ProviderUnavailable("everything is down")
            yield  # pragma: no cover - makes this an async generator

    model_catalog._providers = {"scripted": DeadProvider([CHAT_MODEL])}     # noqa: SLF001
    await model_catalog.refresh(force=True)

    _, events = await collect(core, "Hallo")
    failed = next(e for e in events if e.type == "turn.failed")
    assert failed.data["recovery"] in {"alternative", "retry", "needs_user", "fatal"}
    assert failed.data["user_message"]


async def test_error_turns_are_excluded_from_later_history(wired) -> None:
    """A failed turn must not be replayed to the model as if it were a real answer."""
    core, provider, store, model_catalog = wired
    conversation_id = await store.create()
    await store.add_message(conversation_id, "user", "kaputte frage")
    await store.add_message(conversation_id, "assistant", "Fehler", error="boom")
    provider._script = [text_turn("Neue Antwort")]                          # noqa: SLF001

    await collect(core, "neue frage", conversation_id=conversation_id)
    contents = " ".join(m.content for m in provider.calls[0]["messages"])
    assert "Fehler" not in contents


# --- HTTP surface --------------------------------------------------------------------------


@pytest.fixture
def client(jarvis_home: Path) -> Iterator[TestClient]:
    import app as app_module
    from memory.database import db

    db.set_path(jarvis_home / "data" / "jarvis.db")
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_models_endpoint_defaults_to_free_only(client: TestClient) -> None:
    payload = client.get("/api/models").json()
    assert payload["free_only"] is True
    assert all(m["is_free"] for m in payload["models"])


def test_settings_endpoint_never_returns_secret_values(client: TestClient) -> None:
    payload = client.get("/api/settings").json()
    assert "settings" in payload
    for secret in payload["secrets"]:
        assert "value" not in secret
        assert set(secret) == {"name", "configured", "preview", "source"}


def test_settings_update_round_trip(client: TestClient) -> None:
    response = client.put("/api/settings", json={"patch": {"assistant": {"name": "Friday"}}})
    assert response.status_code == 200
    assert response.json()["settings"]["assistant"]["name"] == "Friday"
    assert client.get("/api/settings").json()["settings"]["assistant"]["name"] == "Friday"


def test_permissions_reject_unknown_capabilities(client: TestClient) -> None:
    response = client.put("/api/permissions", json={"permissions": {"not_a_capability": "ALLOW"}})
    assert response.status_code == 400


def test_setup_endpoint_reports_hardware_and_gaps(client: TestClient) -> None:
    payload = client.get("/api/setup").json()
    assert payload["first_run_completed"] is False
    assert "hardware" in payload and "recommendations" in payload
    assert "whisper_model" in payload["recommendations"]


def test_chat_stream_emits_sse_frames(client: TestClient) -> None:
    """Without a provider configured the turn fails — but it must fail as valid SSE."""
    with client.stream("POST", "/api/chat/stream", json={"message": "Hallo"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())
    frames = [line[6:] for line in body.splitlines() if line.startswith("data: ")]
    assert frames[-1] == "[DONE]"
    parsed = [json.loads(f) for f in frames[:-1]]
    assert parsed[0]["type"] == "turn.started"
    assert any(p["type"] in ("turn.failed", "turn.completed") for p in parsed)
