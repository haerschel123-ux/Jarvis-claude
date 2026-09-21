"""Rotating local backups (Spec §88).

Backs up the database, the settings and an export of the memories. Secrets are excluded by
default — a backup file is exactly the sort of thing that ends up in a synced folder.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from core.logging_setup import get_logger
from core.paths import PATHS

log = get_logger("backup")

PREFIX = "jarvis-backup-"


@dataclass(slots=True)
class BackupInfo:
    path: Path
    created_at: float
    size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.path.name,
            "path": str(self.path),
            "created_at": self.created_at,
            "size": self.size,
        }


class BackupManager:
    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        return self._directory or PATHS.backups

    async def create(self, settings: Settings | None = None, *, label: str = "") -> BackupInfo:
        settings = settings or get_settings()
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        suffix = f"-{label}" if label else ""
        target = self.directory / f"{PREFIX}{stamp}{suffix}.zip"

        # The database is copied through SQLite's own backup API so a concurrent write does
        # not produce a torn file.
        snapshot = await self._snapshot_database()

        try:
            payload = await self._collect(settings)
            await asyncio.to_thread(self._write_archive, target, snapshot, payload, settings)
        finally:
            if snapshot is not None and snapshot.exists():
                snapshot.unlink(missing_ok=True)

        info = BackupInfo(target, time.time(), target.stat().st_size)
        log.info("Backup erstellt: %s (%.1f KB)", target.name, info.size / 1024)
        await self.rotate(settings)
        return info

    async def _snapshot_database(self) -> Path | None:
        from memory.database import db

        if not db.path.exists():
            return None
        snapshot = self.directory / f".snapshot-{int(time.time())}.db"
        try:
            connection = await db.connect()
            await connection.execute("VACUUM INTO ?", (str(snapshot),))
            return snapshot
        except Exception:
            # VACUUM INTO needs SQLite 3.27+; fall back to a plain copy, which is still
            # consistent enough for a personal backup while WAL is checkpointed.
            log.debug("VACUUM INTO nicht verfügbar, kopiere die Datei", exc_info=True)
            try:
                shutil.copy2(db.path, snapshot)
                return snapshot
            except OSError:
                log.warning("Datenbank konnte nicht gesichert werden")
                return None

    async def _collect(self, settings: Settings) -> dict[str, Any]:
        from memory.manager import memory_manager

        try:
            memories = await memory_manager.export()
        except Exception:
            log.exception("Gedächtnis-Export fehlgeschlagen")
            memories = {"version": 1, "count": 0, "memories": []}
        return {"memories": memories}

    def _write_archive(
        self, target: Path, snapshot: Path | None, payload: dict[str, Any], settings: Settings
    ) -> None:
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({
                "created_at": datetime.now(UTC).isoformat(),
                "includes_secrets": settings.backups.include_secrets,
                "assistant": settings.assistant.name,
            }, indent=2, ensure_ascii=False))

            archive.writestr("settings.json",
                             json.dumps(settings.model_dump(mode="json"), indent=2, ensure_ascii=False))
            archive.writestr("memories.json",
                             json.dumps(payload["memories"], indent=2, ensure_ascii=False, default=str))
            if snapshot is not None and snapshot.exists():
                archive.write(snapshot, "jarvis.db")

            # Secrets are only included when the user explicitly turned that on (Spec §88).
            if settings.backups.include_secrets and PATHS.secrets_file.exists():
                archive.write(PATHS.secrets_file, "secrets.json")

    async def rotate(self, settings: Settings | None = None) -> int:
        settings = settings or get_settings()
        keep = max(int(settings.backups.keep_last), 1)
        backups = self.list()
        removed = 0
        for info in backups[keep:]:
            try:
                info.path.unlink()
                removed += 1
            except OSError:
                log.warning("Backup %s konnte nicht gelöscht werden", info.path.name)
        if removed:
            log.info("%d alte Backup(s) entfernt", removed)
        return removed

    def list(self) -> list[BackupInfo]:
        if not self.directory.exists():
            return []
        entries: list[BackupInfo] = []
        for path in self.directory.glob(f"{PREFIX}*.zip"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append(BackupInfo(path, stat.st_mtime, stat.st_size))
        return sorted(entries, key=lambda item: item.created_at, reverse=True)

    def latest(self) -> BackupInfo | None:
        backups = self.list()
        return backups[0] if backups else None

    async def is_due(self, settings: Settings | None = None) -> bool:
        settings = settings or get_settings()
        if not settings.backups.enabled:
            return False
        latest = self.latest()
        if latest is None:
            return True
        return (time.time() - latest.created_at) > settings.backups.interval_hours * 3600

    def inspect(self, path: Path) -> dict[str, Any]:
        """Read a backup's manifest without extracting it."""
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("manifest.json")) if "manifest.json" in names else {}
        return {"name": path.name, "entries": names, "manifest": manifest}


backups = BackupManager()
