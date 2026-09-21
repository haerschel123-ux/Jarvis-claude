"""Secret redaction (Spec §63).

Used by the logging stack, the audit trail and anywhere tool output is shown to a model or
to the user. The goal is to make it hard to leak a credential by accident, not to provide a
cryptographic guarantee — so the patterns err on the side of redacting too much.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "***REDACTED***"

# Ordered most-specific first. Each pattern keeps the identifying prefix and masks the value
# so logs stay useful ("Authorization: Bearer ***REDACTED***").
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\b(authorization\s*:\s*)(bearer|basic|token)\s+\S+"), r"\1\2 " + MASK),
    (
        # An optional prefix lets this match PREFIXED names too, e.g. OPENROUTER_API_KEY or
        # GITHUB_TOKEN, where a plain word boundary before "api"/"token" would never match.
        re.compile(
            r"(?i)([A-Za-z0-9_.-]*(?:api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|"
            r"session[_-]?token|auth[_-]?token|bot[_-]?token|token|secret|password|passwd|"
            r"client[_-]?secret|private[_-]?key|bearer))"
            # Separator: an optional closing quote of the key, "=" or ":", then an optional
            # opening quote of the value. Covers env vars, YAML, JSON and query strings alike.
            r"([\"']?\s*[=:]\s*[\"']?)"
            r"([^\s,;\"'}\]&]{4,})"
        ),
        r"\1\2" + MASK,
    ),
    # Well-known token shapes, redacted even without a labelled key.
    (re.compile(r"\bsk-or-v1-[A-Za-z0-9_-]{20,}"), MASK),          # OpenRouter
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), MASK),                   # OpenAI-style
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), MASK),            # GitHub
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), MASK),          # GitHub fine-grained
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), MASK),          # Slack
    (re.compile(r"\bAIza[A-Za-z0-9_-]{30,}"), MASK),                # Google API
    (re.compile(r"\bya29\.[A-Za-z0-9_-]{20,}"), MASK),              # Google OAuth
    (re.compile(r"\bmfa\.[A-Za-z0-9_-]{60,}"), MASK),               # Discord MFA
    (re.compile(r"\b[MNO][A-Za-z0-9]{23,26}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,}"), MASK),  # Discord bot
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), MASK),  # JWT
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), MASK),
]

# Keys whose *value* is always masked wherever they appear in a dict.
_SENSITIVE_KEYS = frozenset(
    {
        "api_key", "apikey", "api-key", "token", "access_token", "refresh_token",
        "secret", "client_secret", "password", "passwd", "pwd", "authorization",
        "auth", "private_key", "session_token", "bearer", "credential", "credentials",
        "openrouter_api_key", "github_token", "discord_bot_token", "nitrado_token",
        "home_assistant_token", "pairing_code", "device_token",
    }
)


def redact_text(text: str) -> str:
    """Mask credential-looking substrings in free text."""
    if not text:
        return text
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact_value(value: Any, _depth: int = 0) -> Any:
    """Recursively redact a JSON-like structure.

    Values under a sensitive key are masked wholesale; every other string is scanned for
    credential patterns.
    """
    if _depth > 12:
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.strip().lower().replace(" ", "_") in _SENSITIVE_KEYS:
                out[key] = MASK if item not in (None, "") else item
            else:
                out[key] = redact_value(item, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        rendered = [redact_value(item, _depth + 1) for item in value]
        return type(value)(rendered) if isinstance(value, tuple) else rendered
    return value


def mask_secret(value: str | None, keep: int = 4) -> str:
    """Render a secret for display: ``sk-or-v1-...a1b2``. Never returns the full value."""
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * 8 + value[-keep:]
