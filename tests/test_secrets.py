"""Credential storage must never leak a value and must degrade safely (Spec §62, §91)."""

from __future__ import annotations

import os
import stat

from core.secrets import SecretStore


def test_roundtrip_and_delete(secrets: SecretStore) -> None:
    assert secrets.get("openrouter_api_key") is None
    secrets.set("openrouter_api_key", "sk-or-v1-testvalue1234567890")
    assert secrets.get("openrouter_api_key") == "sk-or-v1-testvalue1234567890"
    secrets.delete("openrouter_api_key")
    assert secrets.get("openrouter_api_key") is None


def test_file_backend_is_owner_only(secrets: SecretStore) -> None:
    secrets.set("nitrado_token", "abcdef123456")
    mode = stat.S_IMODE(os.stat(secrets.file_path).st_mode)
    assert mode == 0o600


def test_describe_never_returns_the_raw_value(secrets: SecretStore) -> None:
    secrets.set("github_token", "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123")
    described = secrets.describe("github_token")
    assert described["configured"] is True
    assert "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123" not in str(described)
    assert described["preview"].endswith("0123")


def test_environment_is_used_as_a_fallback(secrets: SecretStore, monkeypatch) -> None:
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "env-provided-token")
    assert secrets.get("discord_bot_token") == "env-provided-token"
    assert secrets.describe("discord_bot_token")["source"] == "environment"


def test_stored_value_wins_over_environment(secrets: SecretStore, monkeypatch) -> None:
    monkeypatch.setenv("NITRADO_TOKEN", "from-env")
    secrets.set("nitrado_token", "from-store")
    assert secrets.get("nitrado_token") == "from-store"


def test_setting_an_empty_value_clears_the_secret(secrets: SecretStore, monkeypatch) -> None:
    # The environment fallback would otherwise mask the deletion on machines that happen to
    # export GITHUB_TOKEN, so it is removed for this test.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    secrets.set("github_token", "something123")
    secrets.set("github_token", "   ")
    assert secrets.get("github_token") is None


def test_describe_all_covers_every_known_secret(secrets: SecretStore) -> None:
    from core.secrets import SECRET_ENV_MAP

    assert {d["name"] for d in secrets.describe_all()} == set(SECRET_ENV_MAP)
