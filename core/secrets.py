"""Credential storage (Spec §62, §91).

Three sources, checked in order of preference:

1. **OS credential store** via :mod:`keyring` — Windows Credential Manager, macOS Keychain,
   Secret Service on Linux. This is what the packaged desktop app uses.
2. **Local file** ``data/secrets.json`` with owner-only permissions — used when no OS
   credential store is available (headless servers, containers).
3. **Environment / .env** — read-only, development convenience only.

Nothing in here ever returns a secret to the chat layer; callers that need to *show*
something use :func:`core.redaction.mask_secret`.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path

from core.paths import PATHS
from core.redaction import mask_secret

KEYRING_SERVICE = "JARVIS"

# Known secret names and the environment variable each one may be seeded from.
SECRET_ENV_MAP: dict[str, str] = {
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "custom_openai_api_key": "CUSTOM_OPENAI_API_KEY",
    "github_token": "GITHUB_TOKEN",
    "discord_bot_token": "DISCORD_BOT_TOKEN",
    "nitrado_token": "NITRADO_TOKEN",
    "home_assistant_token": "HOME_ASSISTANT_TOKEN",
    "google_client_secret": "GOOGLE_CLIENT_SECRET",
}


def _load_keyring():
    """Import keyring lazily; returns None when unavailable or unusable.

    A missing or broken credential store must never stop JARVIS from starting (Spec §114).
    """
    try:
        import keyring
        from keyring.errors import KeyringError

        backend = keyring.get_keyring()
        name = type(backend).__name__
        # The "fail" and "null" backends silently discard writes — treat them as absent.
        if "Fail" in name or "Null" in name:
            return None
        _ = KeyringError  # imported to prove the module is functional
        return keyring
    except Exception:
        return None


class SecretStore:
    """Thread-safe façade over the available credential backends."""

    def __init__(self, file_path: Path | None = None, use_keyring: bool = True) -> None:
        self._file_path = file_path
        self._lock = threading.RLock()
        self._keyring = _load_keyring() if use_keyring else None
        self._cache: dict[str, str] | None = None

    @property
    def file_path(self) -> Path:
        return self._file_path or PATHS.secrets_file

    @property
    def backend_name(self) -> str:
        if self._keyring is not None:
            try:
                return f"keyring:{type(self._keyring.get_keyring()).__name__}"
            except Exception:
                return "keyring"
        return "file"

    # --- file backend ---------------------------------------------------------------

    def _read_file(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        data: dict[str, str] = {}
        path = self.file_path
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = {str(k): str(v) for k, v in loaded.items() if v is not None}
            except (json.JSONDecodeError, OSError):
                data = {}
        self._cache = data
        return data

    def _write_file(self, data: dict[str, str]) -> None:
        path = self.file_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — owner only
        except OSError:
            pass
        tmp.replace(path)
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self._cache = data

    # --- public API -----------------------------------------------------------------

    def get(self, name: str) -> str | None:
        """Return the secret, or None. Checks store first, then environment."""
        with self._lock:
            if self._keyring is not None:
                try:
                    value = self._keyring.get_password(KEYRING_SERVICE, name)
                    if value:
                        return value
                except Exception:
                    pass
            value = self._read_file().get(name)
            if value:
                return value
        env_name = SECRET_ENV_MAP.get(name, name.upper())
        env_value = os.environ.get(env_name)
        return env_value or None

    def set(self, name: str, value: str) -> None:
        """Store a secret in the strongest available backend."""
        value = (value or "").strip()
        if not value:
            self.delete(name)
            return
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.set_password(KEYRING_SERVICE, name, value)
                    return
                except Exception:
                    pass  # fall through to the file backend
            data = dict(self._read_file())
            data[name] = value
            self._write_file(data)

    def delete(self, name: str) -> None:
        with self._lock:
            if self._keyring is not None:
                try:
                    self._keyring.delete_password(KEYRING_SERVICE, name)
                except Exception:
                    pass
            data = dict(self._read_file())
            if data.pop(name, None) is not None:
                self._write_file(data)

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def describe(self, name: str) -> dict[str, object]:
        """Safe-to-display status for the settings UI — never the raw value."""
        value = self.get(name)
        return {
            "name": name,
            "configured": bool(value),
            "preview": mask_secret(value) if value else "",
            "source": self._source_of(name) if value else None,
        }

    def _source_of(self, name: str) -> str:
        if self._keyring is not None:
            try:
                if self._keyring.get_password(KEYRING_SERVICE, name):
                    return "credential_store"
            except Exception:
                pass
        if self._read_file().get(name):
            return "file"
        return "environment"

    def describe_all(self) -> list[dict[str, object]]:
        return [self.describe(name) for name in SECRET_ENV_MAP]

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None


secret_store = SecretStore()
