"""Secret redaction must catch credentials without destroying ordinary log lines (Spec §63)."""

from __future__ import annotations

import pytest

from core.redaction import MASK, mask_secret, redact_text, redact_value


@pytest.mark.parametrize(
    "raw",
    [
        "Authorization: Bearer sk-or-v1-abcdefghijklmnopqrstuvwxyz012345",
        "OPENROUTER_API_KEY=supersecretvalue123",
        "GITHUB_TOKEN=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123",
        'api_key: "abcdefgh12345"',
        "password: hunter2xyz",
        "client_secret=abcdefghijklmnop",
        "Here is a token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 please use it",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_credentials_are_masked(raw: str) -> None:
    assert MASK in redact_text(raw)
    assert "supersecretvalue123" not in redact_text(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "Loaded model openrouter/free with context 128000",
        "This is a normal log line about tokens in general.",
        "Reading file D:/Projects/DayZ/types.xml",
        "Wake word detected, opening command window",
    ],
)
def test_ordinary_lines_survive(raw: str) -> None:
    assert redact_text(raw) == raw


def test_nested_structures_are_redacted() -> None:
    payload = {
        "api_key": "abc123456",
        "nested": {"password": "hunter2", "safe": "hello"},
        "list": [{"token": "xyz987654"}, "plain string"],
    }
    result = redact_value(payload)
    assert result["api_key"] == MASK
    assert result["nested"]["password"] == MASK
    assert result["nested"]["safe"] == "hello"
    assert result["list"][0]["token"] == MASK
    assert result["list"][1] == "plain string"


def test_empty_sensitive_value_is_left_alone() -> None:
    assert redact_value({"api_key": ""}) == {"api_key": ""}
    assert redact_value({"api_key": None}) == {"api_key": None}


def test_mask_secret_never_reveals_the_value() -> None:
    secret = "sk-or-v1-abcdefghijk1234"
    masked = mask_secret(secret)
    assert secret not in masked
    assert masked.endswith("1234")
    assert mask_secret("") == ""
    assert mask_secret("ab") == "**"
