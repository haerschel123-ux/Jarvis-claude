"""Shared test fixtures.

Every test runs against a throwaway JARVIS home directory so the developer's real database,
settings and logs are never touched.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from core.config import SettingsStore
from core.logging_setup import reset_logging_for_tests, setup_logging
from core.paths import PATHS
from core.secrets import SecretStore
from memory.database import Database


@pytest.fixture
def jarvis_home(tmp_path: Path) -> Iterator[Path]:
    """Point every runtime path at a temporary directory for the duration of one test."""
    original = PATHS.base
    PATHS.reset(tmp_path)
    PATHS.ensure()
    reset_logging_for_tests()
    setup_logging(console=False)
    try:
        yield tmp_path
    finally:
        reset_logging_for_tests()
        PATHS.reset(original)


@pytest.fixture
async def database(jarvis_home: Path) -> AsyncIterator[Database]:
    """A migrated, isolated database instance."""
    instance = Database(jarvis_home / "data" / "test.db")
    await instance.connect()
    await instance.migrate()
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
def settings_file(jarvis_home: Path) -> SettingsStore:
    return SettingsStore(jarvis_home / "data" / "settings.json")


@pytest.fixture
def secrets(jarvis_home: Path) -> SecretStore:
    """Secret store with the OS keyring disabled, so tests never touch the real one."""
    return SecretStore(jarvis_home / "data" / "secrets.json", use_keyring=False)


@pytest.fixture
def event_loop_policy() -> asyncio.AbstractEventLoopPolicy:
    return asyncio.get_event_loop_policy()
