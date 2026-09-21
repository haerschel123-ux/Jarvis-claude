"""Filesystem layout for JARVIS.

All runtime state lives under a single base directory so that the packaged desktop app,
the development checkout and the test suite can be pointed at different locations without
any other module needing to care.

Resolution order for the base directory:
  1. ``JARVIS_HOME`` environment variable (used by tests and by the packaged app)
  2. the repository root, when running from a source checkout
  3. the per-user application data directory, when running from a frozen build
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_frozen() -> bool:
    """True when running inside a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def _default_base_dir() -> Path:
    if _is_frozen():
        if sys.platform == "win32":
            root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        elif sys.platform == "darwin":
            root = Path.home() / "Library" / "Application Support"
        else:
            root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return root / "JARVIS"
    return PACKAGE_ROOT


class AppPaths:
    """Resolved runtime directories. Instantiated once as ``PATHS`` below."""

    def __init__(self, base: Path | None = None) -> None:
        if base is None:
            env_home = os.environ.get("JARVIS_HOME")
            base = Path(env_home).expanduser().resolve() if env_home else _default_base_dir()
        self.base = base
        self.data = base / "data"
        self.logs = base / "logs"
        self.models = base / "models"
        self.backups = base / "backups"
        self.voices = self.models / "voices"
        self.wakewords = self.models / "wakewords"
        self.whisper = self.models / "whisper"
        # Static web assets always ship with the code, never with the user data.
        self.web = PACKAGE_ROOT / "web"

    @property
    def database(self) -> Path:
        return self.data / "jarvis.db"

    @property
    def settings_file(self) -> Path:
        return self.data / "settings.json"

    @property
    def secrets_file(self) -> Path:
        return self.data / "secrets.json"

    @property
    def app_registry_file(self) -> Path:
        return self.data / "app_registry.json"

    def ensure(self) -> None:
        """Create every runtime directory. Safe to call repeatedly."""
        for directory in (
            self.data,
            self.logs,
            self.models,
            self.backups,
            self.voices,
            self.wakewords,
            self.whisper,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def reset(self, base: Path) -> None:
        """Re-point every path at a new base directory (used by the test suite)."""
        self.__init__(base)


PATHS = AppPaths()
