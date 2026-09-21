"""SQLite access layer and forward-only migration runner (Spec §64).

Design rules:

* The database is **never** dropped or recreated because the schema changed. Every change is
  a new numbered file in ``memory/migrations/``.
* WAL journaling, so the scheduler can write while a chat request reads.
* Foreign keys are enforced — SQLite leaves them off by default.
* One shared connection guarded by a lock. SQLite handles a single writer best, and JARVIS
  is a single-user application, so this avoids a pool's complexity without a throughput cost.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import aiosqlite

from core.logging_setup import get_logger
from core.paths import PATHS

log = get_logger("database")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_NAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class Migration:
    __slots__ = ("version", "name", "sql", "path")

    def __init__(self, version: int, name: str, sql: str, path: Path) -> None:
        self.version = version
        self.name = name
        self.sql = sql
        self.path = path

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Migration {self.version:04d}_{self.name}>"


def discover_migrations(directory: Path | None = None) -> list[Migration]:
    """Load migration files in version order, rejecting malformed or duplicate versions."""
    directory = directory or MIGRATIONS_DIR
    migrations: list[Migration] = []
    seen: set[int] = set()
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise ValueError(
                f"Migration file '{path.name}' does not follow the NNNN_name.sql convention"
            )
        version = int(match.group(1))
        if version in seen:
            raise ValueError(f"Duplicate migration version {version} in {directory}")
        seen.add(version)
        migrations.append(Migration(version, match.group(2), path.read_text(encoding="utf-8"), path))
    return migrations


class Database:
    """Async SQLite wrapper. A single instance is shared process-wide as ``db``."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        return self._path or PATHS.database

    def set_path(self, path: Path) -> None:
        """Point the database at a different file. Only valid while disconnected."""
        if self._conn is not None:
            raise RuntimeError("Cannot change the database path while connected")
        self._path = path

    @property
    def is_connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self.path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        return conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _connection(self) -> aiosqlite.Connection:
        return self._conn or await self.connect()

    # --- queries --------------------------------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Run a statement and return ``lastrowid`` (0 for statements without one)."""
        conn = await self._connection()
        async with self._lock:
            cursor = await conn.execute(sql, tuple(params or ()))
            try:
                return cursor.lastrowid or 0
            finally:
                await cursor.close()

    async def execute_many(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        conn = await self._connection()
        async with self._lock:
            await conn.executemany(sql, [tuple(r) for r in rows])

    async def fetch_one(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        conn = await self._connection()
        async with self._lock:
            cursor = await conn.execute(sql, tuple(params or ()))
            try:
                row = await cursor.fetchone()
            finally:
                await cursor.close()
        return dict(row) if row is not None else None

    async def fetch_all(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]:
        conn = await self._connection()
        async with self._lock:
            cursor = await conn.execute(sql, tuple(params or ()))
            try:
                rows = await cursor.fetchall()
            finally:
                await cursor.close()
        return [dict(r) for r in rows]

    async def fetch_value(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = await self.fetch_one(sql, params)
        return next(iter(row.values())) if row else None

    async def executescript(self, script: str) -> None:
        conn = await self._connection()
        async with self._lock:
            await conn.executescript(script)

    # --- migrations -----------------------------------------------------------------

    async def _ensure_migration_table(self) -> None:
        conn = await self._connection()
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     INTEGER PRIMARY KEY,
                name        TEXT NOT NULL,
                applied_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )

    async def applied_versions(self) -> set[int]:
        await self._ensure_migration_table()
        rows = await self.fetch_all("SELECT version FROM schema_migrations")
        return {int(r["version"]) for r in rows}

    async def migrate(self, directory: Path | None = None) -> list[int]:
        """Apply every pending migration inside a transaction. Returns applied versions."""
        conn = await self._connection()
        await self._ensure_migration_table()
        applied = await self.applied_versions()
        pending = [m for m in discover_migrations(directory) if m.version not in applied]
        if not pending:
            log.debug("Database schema is up to date (version %s)", max(applied, default=0))
            return []

        done: list[int] = []
        for migration in pending:
            log.info("Applying migration %04d_%s", migration.version, migration.name)
            # executescript() implicitly commits any open transaction, so the transaction has
            # to live *inside* the script rather than around it. The bookkeeping INSERT goes
            # in there too, so a failed migration leaves neither schema changes nor a version
            # row behind. The name is constrained to [a-z0-9_]+ by the filename regex, so
            # interpolating it here cannot inject SQL.
            script = (
                "BEGIN;\n"
                f"{migration.sql}\n"
                "INSERT INTO schema_migrations (version, name) VALUES "
                f"({migration.version}, '{migration.name}');\n"
                "COMMIT;\n"
            )
            async with self._lock:
                try:
                    await conn.executescript(script)
                except Exception:
                    try:
                        await conn.execute("ROLLBACK")
                    except Exception:
                        pass  # the failure may already have rolled the transaction back
                    log.exception(
                        "Migration %04d_%s failed; database left unchanged",
                        migration.version,
                        migration.name,
                    )
                    raise
            done.append(migration.version)
        log.info("Applied %d migration(s), schema now at version %d", len(done), done[-1])
        return done

    async def schema_version(self) -> int:
        await self._ensure_migration_table()
        value = await self.fetch_value("SELECT MAX(version) FROM schema_migrations")
        return int(value or 0)

    # --- key/value helpers ----------------------------------------------------------

    async def kv_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetch_one("SELECT value FROM settings_kv WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    async def kv_set(self, key: str, value: Any) -> None:
        await self.execute(
            """
            INSERT INTO settings_kv (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False, default=str)),
        )

    async def table_names(self) -> list[str]:
        rows = await self.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        )
        return [r["name"] for r in rows]


db = Database()
