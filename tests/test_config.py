"""Settings defaults, merging and resilience (Spec §7, §15, §26, §92)."""

from __future__ import annotations

import json

from core.config import Settings, SettingsStore, default_permissions
from core.enums import AutonomyLevel, Capability, MemoryMode, PermissionValue, RouterMode


def test_specified_defaults() -> None:
    s = Settings()
    assert s.models.free_only is True                       # Spec §7: FREE ONLY = ON
    assert s.models.free_router_model == "openrouter/free"  # Spec §9
    assert s.memory.mode is MemoryMode.AUTO                 # Spec §26
    assert s.assistant.autonomy_level is AutonomyLevel.ASK_RISKY
    assert s.voice.save_voice_recordings is False           # Spec §6
    assert s.server.host == "127.0.0.1"                     # Spec §92
    assert s.assistant.name == "JARVIS"
    assert s.assistant.language == "de"
    assert s.models.router_mode is RouterMode.AUTO


def test_every_capability_has_a_permission() -> None:
    perms = default_permissions()
    assert set(perms) == {c.value for c in Capability}
    # Spec §44 — autonomous research is wanted, so web search is allowed by default.
    assert perms[Capability.WEB_SEARCH.value] is PermissionValue.ALLOW
    # Anything destructive or outward-facing asks first.
    for capability in (Capability.FILE_DELETE, Capability.EMAIL_SEND, Capability.GIT_PUSH):
        assert perms[capability.value] is PermissionValue.ASK


def test_update_deep_merges_and_persists(settings_file: SettingsStore) -> None:
    settings_file.load()
    updated = settings_file.update({"assistant": {"name": "Friday"}})
    assert updated.assistant.name == "Friday"
    # A partial update must not reset unrelated values in the same section.
    assert updated.assistant.autonomy_level is AutonomyLevel.ASK_RISKY
    assert updated.models.free_only is True

    reloaded = SettingsStore(settings_file.path).load()
    assert reloaded.assistant.name == "Friday"


def test_unknown_capability_in_file_is_dropped_and_missing_ones_filled() -> None:
    s = Settings.model_validate({"permissions": {"file_read": "DENY", "obsolete_thing": "ALLOW"}})
    assert s.permissions["file_read"] is PermissionValue.DENY
    assert "obsolete_thing" not in s.permissions
    assert set(s.permissions) == {c.value for c in Capability}


def test_corrupt_settings_file_falls_back_to_defaults(settings_file: SettingsStore) -> None:
    """A broken file must not stop JARVIS from starting (Spec §114)."""
    settings_file.path.parent.mkdir(parents=True, exist_ok=True)
    settings_file.path.write_text("{ this is not json", encoding="utf-8")
    settings = settings_file.load()
    assert settings.assistant.name == "JARVIS"
    assert settings_file.path.with_suffix(".broken.json").exists()


def test_blank_assistant_name_falls_back(settings_file: SettingsStore) -> None:
    assert settings_file.update({"assistant": {"name": "   "}}).assistant.name == "JARVIS"


def test_wake_words_are_normalised() -> None:
    s = Settings.model_validate({"voice": {"wake_words": ["  Jarvis ", "", "Computer"]}})
    assert s.voice.wake_words == ["jarvis", "computer"]
    assert Settings.model_validate({"voice": {"wake_words": []}}).voice.wake_words == ["jarvis"]


def test_settings_survive_a_json_roundtrip() -> None:
    dumped = json.loads(Settings().model_dump_json())
    assert Settings.model_validate(dumped) == Settings()
