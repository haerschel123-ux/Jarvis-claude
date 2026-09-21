"""Guard against memorising credentials (Spec §25).

The memory pipeline runs on the user's own conversations, which is exactly where an API key
or a password is most likely to be pasted. Anything that looks like a credential, or is
*about* a credential, is refused outright — a memory is long-lived and gets injected into
future prompts, so a false positive costs nothing while a false negative leaks a secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from core.redaction import MASK, redact_text

# Phrases that mark the *subject* as a credential even when no token-shaped string appears,
# e.g. "mein Passwort für den Server ist das übliche".
_SENSITIVE_TOPICS = re.compile(
    r"(?i)\b("
    r"passwor[dt]|passwort|kennwort|passphrase|pin|"
    r"api[\s_-]?key|api[\s_-]?schlüssel|zugangsdaten|zugangsschlüssel|"
    r"secret|geheim(?:nis|schlüssel)|token|zugangstoken|"
    r"private[\s_-]?key|privater[\s_-]?schlüssel|ssh[\s_-]?key|"
    r"seed[\s_-]?phrase|recovery[\s_-]?code|2fa|mfa|otp|einmalcode|"
    r"kreditkarte|iban|bankverbindung|sozialversicherung|steuer[\s_-]?id"
    r")\b"
)

# High-entropy strings that are almost certainly a key even without a label.
_TOKEN_SHAPES = re.compile(
    r"(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{16,}|"
    r"xox[abprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{20,}|ya29\.[A-Za-z0-9_-]{15,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)

# A long unbroken run of mixed-case letters and digits is suspicious in prose.
_ENTROPY_RUN = re.compile(r"\b(?=[A-Za-z0-9_-]{24,}\b)(?=[^\s]*[A-Z])(?=[^\s]*[a-z])(?=[^\s]*\d)[A-Za-z0-9_-]{24,}\b")


@dataclass(slots=True)
class FilterVerdict:
    allowed: bool
    reason: str = ""
    matched: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.allowed


def check(text: str, subject: str = "") -> FilterVerdict:
    """Decide whether a candidate memory may be stored."""
    combined = f"{subject} {text}".strip()
    if not combined:
        return FilterVerdict(False, "leerer Inhalt")

    if match := _TOKEN_SHAPES.search(combined):
        return FilterVerdict(False, "enthält einen Zugangsschlüssel", match.group(0)[:12] + "…")

    if match := _SENSITIVE_TOPICS.search(combined):
        return FilterVerdict(False, "handelt von Zugangsdaten", match.group(0))

    if match := _ENTROPY_RUN.search(combined):
        # Long random-looking strings are refused, but ordinary long identifiers such as
        # file paths or model names contain separators and are not matched by this pattern.
        return FilterVerdict(False, "enthält eine schlüsselartige Zeichenfolge", match.group(0)[:12] + "…")

    # Last line of defence: if the redactor would mask something, do not store it.
    if MASK in redact_text(combined):
        return FilterVerdict(False, "wurde von der Maskierung erfasst")

    return FilterVerdict(True)


def is_safe(text: str, subject: str = "") -> bool:
    return check(text, subject).allowed
