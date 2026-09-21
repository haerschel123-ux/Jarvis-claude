"""Migration runner and schema guarantees (Spec §64)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memory.database import Database, discover_migrations

# Every table the specification requires, mapped to the name used in the schema.
REQUIRED_TABLES = {
    "local_profile", "conversations", "messages", "memories", "projects", "project_files",
    "tasks", "task_steps", "reminders", "automations", "tool_runs", "agents", "integrations",
    "settings_kv", "devices", "audit_log", "knowledge_documents", "app_registry",
    "voice_commands",
}


async def test_migration_creates_every_required_table(database: Database) -> None:
    tables = set(await database.table_names())
    assert REQUIRED_TABLES.issubset(tables), f"missing: {REQUIRED_TABLES - tables}"


async def test_migrations_are_idempotent(database: Database) -> None:
    assert await database.migrate() == []
    version_before = await database.schema_version()
    assert await database.migrate() == []
    assert await database.schema_version() == version_before


async def test_existing_data_survives_a_second_migrate(database: Database) -> None:
    """A schema check must never wipe user data (Spec §64)."""
    await database.execute("INSERT INTO projects (name) VALUES (?)", ("DayZ Server",))
    await database.migrate()
    rows = await database.fetch_all("SELECT name FROM projects")
    assert [r["name"] for r in rows] == ["DayZ Server"]


async def test_foreign_keys_are_enforced(database: Database) -> None:
    import aiosqlite

    with pytest.raises(aiosqlite.IntegrityError):
        await database.execute(
            "INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)",
            (9999, "user", "orphan"),
        )


async def test_memory_full_text_search(database: Database) -> None:
    await database.execute(
        "INSERT INTO memories (kind, subject, content) VALUES (?, ?, ?)",
        ("preference", "DayZ", "Der Benutzer betreibt einen DayZ Server auf Nitrado."),
    )
    hits = await database.fetch_all(
        "SELECT m.id FROM memories_fts f JOIN memories m ON m.id = f.rowid "
        "WHERE memories_fts MATCH ?",
        ("nitrado",),
    )
    assert len(hits) == 1


async def test_fts_index_follows_updates_and_deletes(database: Database) -> None:
    row_id = await database.execute(
        "INSERT INTO memories (kind, subject, content) VALUES (?, ?, ?)",
        ("fact", "Bot", "Der Discord Bot heisst Killfeed."),
    )
    await database.execute("UPDATE memories SET content = ? WHERE id = ?",
                           ("Der Discord Bot heisst Sentinel.", row_id))
    assert await _fts_count(database, "killfeed") == 0
    assert await _fts_count(database, "sentinel") == 1
    await database.execute("DELETE FROM memories WHERE id = ?", (row_id,))
    assert await _fts_count(database, "sentinel") == 0


async def _fts_count(database: Database, term: str) -> int:
    rows = await database.fetch_all(
        "SELECT rowid FROM memories_fts WHERE memories_fts MATCH ?", (term,)
    )
    return len(rows)


async def test_key_value_store_roundtrip(database: Database) -> None:
    await database.kv_set("preferred", {"model": "openrouter/free", "free": True})
    assert await database.kv_get("preferred") == {"model": "openrouter/free", "free": True}
    assert await database.kv_get("missing", "fallback") == "fallback"


def test_migration_filenames_are_well_formed() -> None:
    migrations = discover_migrations()
    assert migrations, "no migrations found"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))


async def test_failed_migration_leaves_no_version_row(jarvis_home: Path) -> None:
    """A broken migration must roll back completely, schema and bookkeeping alike."""
    broken_dir = jarvis_home / "broken"
    broken_dir.mkdir()
    (broken_dir / "0001_ok.sql").write_text("CREATE TABLE alpha (id INTEGER PRIMARY KEY);")
    (broken_dir / "0002_bad.sql").write_text(
        "CREATE TABLE beta (id INTEGER PRIMARY KEY);\nTHIS IS NOT SQL;"
    )
    instance = Database(jarvis_home / "data" / "broken.db")
    await instance.connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            await instance.migrate(broken_dir)
        assert await instance.schema_version() == 1
        tables = await instance.table_names()
        assert "alpha" in tables
        assert "beta" not in tables
    finally:
        await instance.close()
