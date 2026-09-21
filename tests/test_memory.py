"""Memory pipeline, retrieval and the secret filter (Spec §24, §25, §26)."""

from __future__ import annotations

import pytest

from core.config import Settings
from core.enums import MemoryKind, MemoryMode
from memory import secrets_filter
from memory.database import Database
from memory.manager import MemoryCandidate, MemoryManager, extract_candidates, parse_model_candidates
from memory.retrieval import MemoryRetrieval, build_match_query, similarity


@pytest.fixture
def manager(database: Database) -> MemoryManager:
    return MemoryManager(database, MemoryRetrieval(database))


def auto_settings(**overrides) -> Settings:
    settings = Settings()
    settings.memory.mode = MemoryMode.AUTO
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(settings, section), field, value)
    return settings


# --- the secret filter ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Mein OpenRouter Key ist sk-or-v1-abcdefghijklmnopqrstuvwxyz1234",
        "Das Passwort für den Server ist hunter2",
        "Der Token lautet ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        "Meine PIN ist 4711",
        "Die IBAN lautet DE89370400440532013000",
        "seed phrase: apple banana cherry dog elephant frog",
        "Hier ist mein private key für den Server",
    ],
)
def test_credentials_are_never_memorised(text: str) -> None:
    """Spec §25: passwords, keys and tokens must never become a memory."""
    assert secrets_filter.check(text).allowed is False


@pytest.mark.parametrize(
    "text",
    [
        "Der Benutzer nutzt VS Code als Editor.",
        "Das DayZ-Projekt liegt unter D:/Projects/DayZ und läuft auf Nitrado.",
        "Der Discord Bot heißt Killfeed und nutzt discord.py 2.4.",
        "Antworte grundsätzlich auf Deutsch, Code auf Englisch.",
        "Die Datenbank heißt jarvis.db im data-Ordner.",
    ],
)
def test_ordinary_facts_are_allowed(text: str) -> None:
    assert secrets_filter.check(text).allowed is True


# --- extraction ------------------------------------------------------------------------------


def test_explicit_memory_request_is_captured() -> None:
    candidates = extract_candidates("Merke dir, dass ich lieber Tabs als Spaces benutze.")
    assert len(candidates) == 1
    assert candidates[0].importance >= 0.9


def test_preferences_and_projects_are_recognised() -> None:
    kinds = {
        c.kind
        for text in (
            "Ich bevorzuge kurze Antworten auf Deutsch.",
            "Mein DayZ Server läuft auf Nitrado.",
            "Ich heiße Sven.",
        )
        for c in extract_candidates(text)
    }
    assert MemoryKind.PREFERENCE in kinds
    assert MemoryKind.PROJECT in kinds
    assert MemoryKind.ENTITY in kinds


def test_extracted_statements_are_grammatical_german() -> None:
    """Rewriting first person to third person by regex produces broken conjugations."""
    candidate = extract_candidates("Ich bevorzuge kurze Antworten auf Deutsch.")[0]
    assert "bevorzuge" in candidate.content
    assert "Der Benutzer bevorzuge" not in candidate.content


def test_momentary_statements_are_not_memorised() -> None:
    assert extract_candidates("Ich nutze das jetzt gerade nur zum Testen.") == []
    assert extract_candidates("Wie spät ist es?") == []


def test_only_the_users_own_words_are_mined() -> None:
    """A model hallucination must not become a durable fact about the user."""
    candidates = extract_candidates(
        "Was kannst du?", assistant_text="Ich bevorzuge es, alles zu löschen."
    )
    assert candidates == []


def test_model_candidates_are_parsed_tolerantly() -> None:
    raw = 'Gerne! {"memories": [{"kind": "preference", "subject": "Editor", ' \
          '"content": "Der Benutzer nutzt Neovim.", "importance": 0.8}]}'
    candidates = parse_model_candidates(raw)
    assert len(candidates) == 1
    assert candidates[0].kind is MemoryKind.PREFERENCE
    assert candidates[0].confidence < 0.85       # model judgement is trusted less than a rule
    assert parse_model_candidates("kein json hier") == []


# --- the pipeline -------------------------------------------------------------------------------


async def test_a_candidate_is_stored(manager: MemoryManager) -> None:
    result = await manager.consider(
        MemoryCandidate("Der Benutzer nutzt VS Code.", "Editor", importance=0.8),
        auto_settings(),
    )
    assert result["action"] == "create"
    assert (await manager.list())[0]["content"] == "Der Benutzer nutzt VS Code."


async def test_the_pipeline_blocks_a_credential(manager: MemoryManager) -> None:
    result = await manager.consider(
        MemoryCandidate("Mein Passwort ist hunter2xyz.", "Passwort", importance=0.95),
        auto_settings(),
    )
    assert result["action"] == "blocked"
    assert await manager.list() == []


async def test_an_identical_candidate_is_skipped(manager: MemoryManager) -> None:
    candidate = MemoryCandidate("Der Benutzer nutzt VS Code als Editor.", "Editor", importance=0.8)
    assert (await manager.consider(candidate, auto_settings()))["action"] == "create"
    assert (await manager.consider(candidate, auto_settings()))["action"] == "skip"
    assert len(await manager.list()) == 1


async def test_a_similar_candidate_updates_instead_of_duplicating(manager: MemoryManager) -> None:
    await manager.consider(
        MemoryCandidate("Der DayZ Server läuft auf Nitrado.", "DayZ", importance=0.8),
        auto_settings(),
    )
    result = await manager.consider(
        MemoryCandidate("Der DayZ Server läuft auf Nitrado mit der Map Sakhal.", "DayZ",
                        importance=0.8),
        auto_settings(),
    )
    assert result["action"] == "update"
    memories = await manager.list()
    assert len(memories) == 1
    assert "Sakhal" in memories[0]["content"]


async def test_memory_mode_off_stores_nothing(manager: MemoryManager) -> None:
    settings = auto_settings()
    settings.memory.mode = MemoryMode.OFF
    result = await manager.consider(
        MemoryCandidate("Der Benutzer nutzt VS Code.", "Editor", importance=0.9), settings
    )
    assert result["action"] == "skip"
    assert await manager.list() == []


async def test_important_only_mode_filters_by_importance(manager: MemoryManager) -> None:
    settings = auto_settings()
    settings.memory.mode = MemoryMode.IMPORTANT_ONLY
    assert (await manager.consider(
        MemoryCandidate("Nebensächlich.", "X", importance=0.5), settings))["action"] == "skip"
    assert (await manager.consider(
        MemoryCandidate("Sehr wichtig für das Projekt.", "Y", importance=0.9),
        settings))["action"] == "create"


async def test_capture_exchange_runs_the_whole_pipeline(manager: MemoryManager) -> None:
    results = await manager.capture_exchange(
        "Merke dir, dass mein Discord Bot Killfeed heißt.",
        "Notiert.",
        settings=auto_settings(),
    )
    assert any(r["action"] == "create" for r in results)
    assert any("Killfeed" in m["content"] for m in await manager.list())


async def test_capture_never_raises_into_the_chat_path(manager: MemoryManager) -> None:
    assert await manager.capture_exchange("", "", settings=auto_settings()) == []


# --- retrieval ------------------------------------------------------------------------------------


async def test_search_finds_stored_memories(manager: MemoryManager) -> None:
    await manager.store(MemoryCandidate(
        "Der DayZ Server läuft auf Nitrado mit der Map Sakhal.", "DayZ Server",
        MemoryKind.PROJECT, 0.9))
    await manager.store(MemoryCandidate(
        "Der Benutzer nutzt VS Code als Editor.", "Editor", MemoryKind.PREFERENCE, 0.7))

    hits = await manager.recall("Was weißt du über meinen DayZ Server?")
    assert hits and "Nitrado" in hits[0]["content"]


async def test_pinned_memories_always_rank_first(manager: MemoryManager) -> None:
    await manager.store(MemoryCandidate("Ganz normaler Eintrag über Katzen.", "Katzen"))
    pinned_id = await manager.store(MemoryCandidate("Angeheftete Grundregel.", "Regel"))
    await manager.update(pinned_id, pinned=True)
    hits = await manager.recall("Katzen")
    assert hits[0]["id"] == pinned_id


async def test_using_a_memory_records_a_hit(manager: MemoryManager) -> None:
    memory_id = await manager.store(MemoryCandidate("Der Bot heißt Killfeed.", "Bot"))
    await manager.recall("Killfeed")
    memory = await MemoryRetrieval(manager._db).by_id(memory_id)   # noqa: SLF001
    assert memory["hits"] >= 1


async def test_search_query_cannot_inject_fts_syntax(manager: MemoryManager) -> None:
    """User text must be data for FTS5, never syntax."""
    await manager.store(MemoryCandidate("Der Bot heißt Killfeed.", "Bot"))
    for nasty in ['killfeed" OR "', "NEAR(a b)", '"; DROP TABLE memories; --', "*", "^"]:
        await manager.recall(nasty)      # must not raise
    assert len(await manager.list()) == 1


def test_stopwords_do_not_dominate_a_query() -> None:
    assert build_match_query("der die das und oder") == ""
    assert "dayz" in build_match_query("Was weißt du über mein DayZ Projekt?")


def test_similarity_behaves_sensibly() -> None:
    assert similarity("Der Server läuft auf Nitrado", "Der Server läuft auf Nitrado") == 1.0
    assert similarity("Der Server läuft auf Nitrado", "Ich nutze VS Code") == 0.0
    assert 0.3 < similarity("Der DayZ Server läuft auf Nitrado",
                            "DayZ Server wird bei Nitrado gehostet") < 1.0


# --- editing, export, import --------------------------------------------------------------------


async def test_editing_a_memory_into_a_secret_is_refused(manager: MemoryManager) -> None:
    memory_id = await manager.store(MemoryCandidate("Harmloser Eintrag.", "Test"))
    with pytest.raises(ValueError, match="abgelehnt"):
        await manager.update(memory_id, content="Mein Passwort ist hunter2xyz")


async def test_export_and_import_round_trip(manager: MemoryManager, database: Database) -> None:
    await manager.store(MemoryCandidate("Der Bot heißt Killfeed.", "Bot", MemoryKind.PROJECT, 0.8))
    exported = await manager.export()
    assert exported["count"] == 1

    other = MemoryManager(database, MemoryRetrieval(database))
    await other.delete(exported["memories"][0]["id"])
    result = await other.import_(exported, auto_settings())
    assert result["created"] == 1
    assert (await other.list())[0]["content"] == "Der Bot heißt Killfeed."


async def test_import_blocks_credentials(manager: MemoryManager) -> None:
    result = await manager.import_(
        {"memories": [{"content": "Mein API Key ist sk-or-v1-abcdefghijklmnop1234", "subject": "Key"},
                      {"content": "Der Bot heißt Killfeed.", "subject": "Bot"}]},
        auto_settings(),
    )
    assert result["blocked"] == 1
    assert result["created"] == 1


async def test_deleting_a_memory(manager: MemoryManager) -> None:
    memory_id = await manager.store(MemoryCandidate("Wegwerfeintrag.", "Test"))
    assert await manager.delete(memory_id) is True
    assert await manager.delete(memory_id) is False
    assert await manager.list() == []
