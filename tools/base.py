"""Tool interface and argument validation (Spec §67, §68).

A tool declares what it is, how dangerous it is and which permission it needs. The registry —
not the tool — enforces the permission, so a tool cannot accidentally skip the check.

Arguments are validated against the tool's JSON Schema **on the server**, never trusted from
the model (Spec §67: "Server validiert immer Argumente"). A deliberately small validator
covers what tool schemas actually use, which keeps the dependency list minimal (Spec §115).
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from core.enums import Capability, RiskLevel
from core.errors import ValidationError
from core.redaction import redact_value
from providers.base import ToolSpec


@dataclass(slots=True)
class ToolResult:
    """What a tool gives back.

    ``content`` goes to the model (as fenced tool output); ``display`` goes to the UI as a
    structured tool card. Keeping them apart means the UI can show a nice summary while the
    model still sees the full data.
    """

    ok: bool = True
    content: str = ""
    display: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    citations: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def failure(cls, message: str, **display: Any) -> ToolResult:
        return cls(ok=False, content=f"FEHLER: {message}", error=message, display=display)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "content": self.content[:4000],
            "display": redact_value(self.display),
            "error": self.error,
            "citations": self.citations,
        }


@dataclass(slots=True)
class ToolContext:
    """Ambient information a tool may need. Never carries credentials."""

    conversation_id: int | None = None
    task_id: int | None = None
    agent: str = ""
    cancel: Any = None          # asyncio.Event, set on cancellation or emergency stop
    # The settings this call runs under. The registry fills this in, so a tool validates
    # paths against the same settings the permission check used rather than re-reading the
    # global store, which could differ (in tests, or mid-update).
    settings: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class Tool(abc.ABC):
    """Base class for every tool."""

    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}}
    risk_level: RiskLevel = RiskLevel.SAFE_READ
    required_permission: Capability = Capability.FILE_READ
    # Set to False for tools that cannot work on this platform; the registry then hides them
    # rather than offering something that will always fail (Spec §108).
    available: bool = True
    unavailable_reason: str = ""

    @abc.abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Do the work. The permission check has already passed when this is called."""

    def summarise(self, arguments: dict[str, Any]) -> str:
        """One line for the confirmation dialog (Spec §69). Override for something clearer."""
        if not arguments:
            return self.description or self.name
        rendered = ", ".join(f"{k}={_short(v)}" for k, v in redact_value(arguments).items())
        return f"{self.name}({rendered})"

    def scope(self, arguments: dict[str, Any]) -> str:
        """Scope key for "always allow here". Default: the tool itself."""
        return self.name

    def risk_for(self, arguments: dict[str, Any]) -> tuple[RiskLevel, Capability]:
        """Risk and capability for *these* arguments.

        The class attributes are the declared ceiling; a tool whose danger depends on its
        input (``run_command`` above all) overrides this so that ``ls`` is not confirmed like
        ``rm -rf``. The refined risk may never exceed the declared ``risk_level``, so a tool
        cannot quietly downgrade itself past the policy the user configured for it.
        """
        return self.risk_level, self.required_permission

    def to_spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.input_schema)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "risk_level": self.risk_level.value,
            "required_permission": self.required_permission.value,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


def _short(value: Any, limit: int = 60) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- argument validation -----------------------------------------------------------------

_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any], tool: str = "") -> dict[str, Any]:
    """Validate and coerce arguments, raising :class:`ValidationError` with a clear message.

    Returns a copy containing only the declared properties, so a model cannot smuggle extra
    keys into a tool implementation.
    """
    if not isinstance(arguments, dict):
        raise ValidationError(
            f"{tool}: arguments must be an object",
            user_message="Die Argumente für dieses Werkzeug waren kein Objekt.",
        )
    if "__malformed_arguments__" in arguments:
        raise ValidationError(
            f"{tool}: model produced malformed JSON arguments",
            user_message="Das Modell hat unvollständige Argumente geliefert.",
        )

    properties: dict[str, Any] = schema.get("properties") or {}
    required: list[str] = schema.get("required") or []
    cleaned: dict[str, Any] = {}
    errors: list[str] = []

    for name in required:
        if name not in arguments or arguments[name] is None:
            errors.append(f"'{name}' fehlt")

    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            continue  # undeclared keys are dropped, not an error — models add stray fields
        if value is None:
            if name not in required:
                continue
            errors.append(f"'{name}' darf nicht null sein")
            continue
        try:
            cleaned[name] = _validate_value(name, value, spec)
        except ValueError as exc:
            errors.append(str(exc))

    for name, spec in properties.items():
        if name not in cleaned and "default" in spec:
            cleaned[name] = spec["default"]

    if errors:
        raise ValidationError(
            f"{tool}: invalid arguments: {'; '.join(errors)}",
            detail={"errors": errors},
            user_message=f"Ungültige Argumente für {tool or 'das Werkzeug'}: {'; '.join(errors)}",
        )
    return cleaned


def _validate_value(name: str, value: Any, spec: dict[str, Any]) -> Any:
    expected = spec.get("type")

    if expected and expected in _TYPES:
        allowed = _TYPES[expected]
        # A model frequently sends numbers and booleans as strings; coerce rather than fail,
        # but only when the coercion is unambiguous.
        if not isinstance(value, allowed) or (expected != "boolean" and isinstance(value, bool)):
            value = _coerce(name, value, expected)

    if (choices := spec.get("enum")) and value not in choices:
        raise ValueError(f"'{name}' muss einer von {choices} sein")

    if expected == "string":
        if (minimum := spec.get("minLength")) is not None and len(value) < minimum:
            raise ValueError(f"'{name}' ist zu kurz (mindestens {minimum})")
        if (maximum := spec.get("maxLength")) is not None and len(value) > maximum:
            raise ValueError(f"'{name}' ist zu lang (höchstens {maximum})")
    elif expected in ("number", "integer"):
        if (minimum := spec.get("minimum")) is not None and value < minimum:
            raise ValueError(f"'{name}' muss mindestens {minimum} sein")
        if (maximum := spec.get("maximum")) is not None and value > maximum:
            raise ValueError(f"'{name}' darf höchstens {maximum} sein")
    elif expected == "array":
        if (maximum := spec.get("maxItems")) is not None and len(value) > maximum:
            raise ValueError(f"'{name}' hat zu viele Einträge (höchstens {maximum})")
        if item_spec := spec.get("items"):
            value = [_validate_value(f"{name}[{i}]", v, item_spec) for i, v in enumerate(value)]

    return value


def _coerce(name: str, value: Any, expected: str) -> Any:
    try:
        if expected == "string":
            if isinstance(value, (dict, list)):
                raise ValueError
            return str(value)
        if expected == "integer":
            if isinstance(value, bool):
                raise ValueError
            return int(str(value).strip())
        if expected == "number":
            if isinstance(value, bool):
                raise ValueError
            return float(str(value).strip())
        if expected == "boolean":
            if isinstance(value, bool):
                return value
            text = str(value).strip().lower()
            if text in ("true", "1", "yes", "ja"):
                return True
            if text in ("false", "0", "no", "nein"):
                return False
            raise ValueError
        if expected == "array" and isinstance(value, str):
            # A single value where a list was expected is a common model slip.
            return [value]
    except (ValueError, TypeError):
        pass
    raise ValueError(f"'{name}' muss vom Typ {expected} sein")
