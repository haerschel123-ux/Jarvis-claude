"""Settings model and persistence.

Settings are plain JSON on disk (``data/settings.json``) so they survive upgrades and can be
inspected by the user. Secrets are deliberately *not* part of this model — they live in
``core.secrets`` and are only ever referenced by name.

Defaults follow the specification: free models only, memory on automatic, proactivity
normal, autonomy at "ask before anything risky", server bound to loopback.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from core.enums import (
    AutonomyLevel,
    Capability,
    CodingMode,
    MemoryMode,
    PermissionValue,
    ProactivityMode,
    RouterMode,
    ScreenMode,
    VoiceMode,
    WakeEngine,
)
from core.paths import PATHS

SETTINGS_VERSION = 1


def default_permissions() -> dict[str, PermissionValue]:
    """Sensible starting point: reading is free, anything that changes the world asks.

    Web search defaults to ALLOW because the specification explicitly requests autonomous
    research (Spec §44).
    """
    allow = {
        Capability.FILE_READ,
        Capability.TERMINAL_READ,
        Capability.SCREEN_CAPTURE,
        Capability.PROGRAM_LAUNCH,
        Capability.WEB_SEARCH,
        Capability.CALENDAR_READ,
        Capability.EMAIL_READ,
    }
    return {c.value: (PermissionValue.ALLOW if c in allow else PermissionValue.ASK) for c in Capability}


class AssistantSettings(BaseModel):
    name: str = "JARVIS"
    language: str = "de"
    personality: str = "calm, competent, technical, direct, politely dry"
    autonomy_level: AutonomyLevel = AutonomyLevel.ASK_RISKY
    proactivity: ProactivityMode = ProactivityMode.NORMAL
    coding_mode: CodingMode = CodingMode.ASK_BEFORE_EDIT
    startup_announcement: bool = True

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        value = value.strip()
        return value or "JARVIS"


class ModelSettings(BaseModel):
    """Model selection policy (Spec §7, §9, §11, §12)."""

    router_mode: RouterMode = RouterMode.AUTO
    free_only: bool = True
    offline_mode: bool = False
    preferred_chat_model: str | None = None
    preferred_coding_model: str | None = None
    preferred_vision_model: str | None = None
    preferred_fast_model: str | None = None
    free_router_model: str = "openrouter/free"
    multi_agent_review: bool = False
    # Per-task-kind capability priorities, highest first (Spec §12).
    priorities: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "coding": ["coding", "tools", "long_context", "speed"],
            "voice_chat": ["speed", "free", "chat"],
            "vision": ["vision", "free", "speed"],
            "research": ["tools", "long_context", "reasoning"],
            "reasoning": ["reasoning", "long_context", "coding"],
        }
    )
    catalog_refresh_hours: int = 12
    request_timeout_seconds: float = 120.0
    max_output_tokens: int = 2048
    temperature: float = 0.7


class ProviderSettings(BaseModel):
    openrouter_enabled: bool = True
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    ollama_enabled: bool = True
    ollama_base_url: str = "http://127.0.0.1:11434"
    custom_enabled: bool = False
    custom_base_url: str = ""
    custom_label: str = "Custom API"
    # Optional attribution headers OpenRouter uses for its app ranking.
    app_referer: str = "https://github.com/haerschel123-ux/Jarvis-claude"
    app_title: str = "JARVIS"


class VoiceSettings(BaseModel):
    mode: VoiceMode = VoiceMode.OFF
    wake_words: list[str] = Field(default_factory=lambda: ["jarvis"])
    wake_engine: WakeEngine = WakeEngine.OPENWAKEWORD
    wake_sensitivity: float = 0.5
    activation_sound: bool = True
    save_voice_recordings: bool = False  # Spec §6 — must default to OFF.
    input_device: str | None = None
    output_device: str | None = None
    stt_model_size: str = "base"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    stt_language: str | None = "de"
    tts_backend: str = "piper"
    tts_voice: str = "de_DE-thorsten-medium"
    tts_speed: float = 1.0
    tts_enabled: bool = True
    command_window_seconds: float = 8.0
    continuous_timeout_seconds: float = 25.0

    @field_validator("wake_words")
    @classmethod
    def _clean_wake_words(cls, value: list[str]) -> list[str]:
        words = [w.strip().lower() for w in value if w and w.strip()]
        return words or ["jarvis"]

    @field_validator("wake_sensitivity")
    @classmethod
    def _clamp_sensitivity(cls, value: float) -> float:
        return min(max(value, 0.0), 1.0)


class ScreenSettings(BaseModel):
    mode: ScreenMode = ScreenMode.SCREENSHOT_ON_REQUEST
    watch_interval_seconds: float = 4.0
    live_assist_fps: float = 0.5
    change_threshold: float = 0.04
    max_width: int = 1280
    ocr_enabled: bool = False
    blur_regions: list[dict[str, int]] = Field(default_factory=list)


class MemorySettings(BaseModel):
    mode: MemoryMode = MemoryMode.AUTO  # Spec §26 — user asked for AUTO.
    max_memories_in_context: int = 12
    importance_threshold: float = 0.45
    secret_filter_enabled: bool = True


class SecuritySettings(BaseModel):
    trusted_folders: list[str] = Field(default_factory=list)
    remote_access_enabled: bool = False
    lan_only: bool = True
    pairing_required: bool = True
    session_timeout_minutes: int = 240
    rate_limit_per_minute: int = 120
    audit_enabled: bool = True


class ServerSettings(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = False


class BackupSettings(BaseModel):
    enabled: bool = True
    keep_last: int = 7
    interval_hours: int = 24
    include_secrets: bool = False  # Spec §88 — never by default.


class UISettings(BaseModel):
    theme: str = "jarvis-dark"
    accent: str = "ice"
    reduced_motion: bool = False
    show_activity_panel: bool = True
    font_scale: float = 1.0


class Settings(BaseModel):
    """Root settings object."""

    version: int = SETTINGS_VERSION
    first_run_completed: bool = False
    assistant: AssistantSettings = Field(default_factory=AssistantSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    providers: ProviderSettings = Field(default_factory=ProviderSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    screen: ScreenSettings = Field(default_factory=ScreenSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    backups: BackupSettings = Field(default_factory=BackupSettings)
    ui: UISettings = Field(default_factory=UISettings)
    permissions: dict[str, PermissionValue] = Field(default_factory=default_permissions)

    @field_validator("permissions")
    @classmethod
    def _fill_missing_permissions(cls, value: dict[str, PermissionValue]) -> dict[str, PermissionValue]:
        """A capability added in a later version must not silently become undefined."""
        merged = default_permissions()
        merged.update({k: v for k, v in value.items() if k in merged})
        return merged

    def permission_for(self, capability: Capability) -> PermissionValue:
        return self.permissions.get(capability.value, PermissionValue.ASK)


class SettingsStore:
    """Loads, caches and atomically saves the settings file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._settings: Settings | None = None

    @property
    def path(self) -> Path:
        return self._path or PATHS.settings_file

    def load(self) -> Settings:
        with self._lock:
            if self._settings is not None:
                return self._settings
            raw: dict[str, Any] = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    # A corrupt settings file must not stop JARVIS from starting.
                    # The broken file is kept for inspection and defaults take over.
                    broken = self.path.with_suffix(".broken.json")
                    try:
                        self.path.replace(broken)
                    except OSError:
                        pass
                    raw = {}
            settings = Settings.model_validate(raw) if raw else Settings()
            settings = _apply_environment_overrides(settings)
            self._settings = settings
            return settings

    def save(self, settings: Settings | None = None) -> Settings:
        with self._lock:
            if settings is not None:
                self._settings = settings
            current = self._settings or Settings()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = current.model_dump(mode="json")
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return current

    def update(self, patch: dict[str, Any]) -> Settings:
        """Deep-merge a partial settings dict, validate it, then persist."""
        with self._lock:
            current = self.load().model_dump(mode="json")
            merged = _deep_merge(current, patch)
            merged["version"] = SETTINGS_VERSION
            validated = Settings.model_validate(merged)
            self._settings = validated
            self.save(validated)
            return validated

    def reload(self) -> Settings:
        with self._lock:
            self._settings = None
            return self.load()


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_environment_overrides(settings: Settings) -> Settings:
    """Environment variables win over the settings file for host/port and base URLs.

    This keeps the development workflow (``.env``) and the packaged app consistent without
    writing machine-specific values back into the user's settings file.
    """
    host = os.environ.get("JARVIS_HOST")
    if host:
        settings.server.host = host
    port = os.environ.get("JARVIS_PORT")
    if port and port.isdigit():
        settings.server.port = int(port)
    ollama = os.environ.get("OLLAMA_BASE_URL")
    if ollama:
        settings.providers.ollama_base_url = ollama.rstrip("/")
    custom = os.environ.get("CUSTOM_OPENAI_BASE_URL")
    if custom:
        settings.providers.custom_base_url = custom.rstrip("/")
        settings.providers.custom_enabled = True
    return settings


settings_store = SettingsStore()


def get_settings() -> Settings:
    return settings_store.load()
