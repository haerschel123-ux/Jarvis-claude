"""Capability scoring for models (Spec §12).

Models are ranked by what they can do and by the user's own priority order — never by a
hardcoded leaderboard, which would go stale the moment a provider ships a new model.

Where a provider reports nothing, the axis scores as *unknown* (a small penalty relative to
a confirmed capability, but far better than a confirmed absence). Every score carries the
reasons that produced it, so the UI can answer "why this model?" (Spec §117).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from providers.base import ModelInfo

# Weight of the n-th entry in the user's priority list. The drop-off is steep on purpose: the
# first priority must genuinely decide, with later ones only breaking ties. A gentler curve
# let a long-context generalist outrank a coding specialist even when "coding" was ranked
# first, which is not what the user asked for.
PRIORITY_WEIGHTS = (1.0, 0.5, 0.25, 0.12, 0.06, 0.03)

UNKNOWN_FACTOR = 0.35   # a capability the provider did not report
MISSING_PENALTY = -0.5  # a capability the provider says is absent

# Name fragments that reliably indicate a small/fast variant. This is an openly-labelled
# heuristic used only for the "speed" axis, which no provider reports numerically.
_FAST_HINTS = re.compile(
    r"(?:^|[-_/:.])(mini|nano|small|tiny|lite|flash|turbo|instant|fast|haiku|8b|7b|4b|3b|2b|1b)(?:$|[-_/:.])",
    re.IGNORECASE,
)
_LARGE_HINTS = re.compile(
    r"(?:^|[-_/:.])(opus|ultra|max|405b|235b|70b|72b|123b|large)(?:$|[-_/:.])", re.IGNORECASE
)
# Name fragments that indicate a coding-specialised model.
_CODING_HINTS = re.compile(r"(cod(?:e|er|ing)|devstral|starcoder|deepseek-?r?\d*|qwen.*coder)", re.IGNORECASE)

LONG_CONTEXT_THRESHOLD = 100_000


@dataclass(slots=True)
class Requirements:
    """Hard requirements a candidate must satisfy to be eligible at all."""

    needs_tools: bool = False
    needs_vision: bool = False
    needs_structured: bool = False
    needs_reasoning: bool = False
    min_context: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "tools": self.needs_tools,
            "vision": self.needs_vision,
            "structured": self.needs_structured,
            "reasoning": self.needs_reasoning,
            "min_context": self.min_context,
        }


@dataclass(slots=True)
class Score:
    model: ModelInfo
    value: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model.key, "score": round(self.value, 3), "reasons": self.reasons}


def axis_value(model: ModelInfo, axis: str) -> float:
    """Score one capability axis in the range -0.5 … 1.0."""
    if axis == "free":
        return 1.0 if model.is_free else 0.0
    if axis == "local":
        return 1.0 if model.is_local else 0.0
    if axis == "long_context":
        if model.context_length is None:
            return UNKNOWN_FACTOR
        return min(model.context_length / LONG_CONTEXT_THRESHOLD, 1.0)
    if axis == "speed":
        return _speed_estimate(model)
    if axis == "coding":
        return _coding_estimate(model)
    if axis == "chat":
        return 1.0  # every chat model can hold a conversation

    flag = {
        "tools": model.supports_tools,
        "vision": model.supports_vision,
        "structured_output": model.supports_structured,
        "reasoning": model.supports_reasoning,
    }.get(axis)
    if flag is None:
        return UNKNOWN_FACTOR
    return 1.0 if flag else MISSING_PENALTY


def _speed_estimate(model: ModelInfo) -> float:
    """Heuristic: no provider publishes latency, so size hints in the name are used instead."""
    name = f"{model.id} {model.name}"
    score = 0.5
    if _FAST_HINTS.search(name):
        score += 0.35
    if _LARGE_HINTS.search(name):
        score -= 0.3
    if model.is_local:
        score += 0.05  # no network round trip
    return max(0.0, min(score, 1.0))


def _coding_estimate(model: ModelInfo) -> float:
    """Coding ability is not a reported field; tool support and naming are the usable signals."""
    score = 0.4
    if _CODING_HINTS.search(f"{model.id} {model.name} {model.description}"):
        score += 0.35
    if model.supports_tools is True:
        score += 0.15
    if (model.context_length or 0) >= LONG_CONTEXT_THRESHOLD:
        score += 0.1
    if _LARGE_HINTS.search(f"{model.id} {model.name}"):
        score += 0.05
    return min(score, 1.0)


def meets(model: ModelInfo, requirements: Requirements) -> tuple[bool, str]:
    """Hard eligibility check. Unknown support fails a hard requirement on purpose."""
    if model.is_router:
        # A router picks a concrete model per request and filters by the features the request
        # needs, so it satisfies every requirement by construction — including context length,
        # which depends on whichever model it ends up choosing.
        return True, ""
    if requirements.needs_tools and model.supports_tools is not True:
        return False, "kein bestätigtes Tool-Calling"
    if requirements.needs_vision and model.supports_vision is not True:
        return False, "keine bestätigte Bildverarbeitung"
    if requirements.needs_structured and model.supports_structured is not True:
        return False, "keine bestätigten strukturierten Ausgaben"
    if requirements.needs_reasoning and model.supports_reasoning is not True:
        return False, "kein bestätigter Reasoning-Modus"
    if requirements.min_context and (model.context_length or 0) < requirements.min_context:
        return False, f"Kontext zu klein (< {requirements.min_context})"
    return True, ""


def score_model(model: ModelInfo, priorities: list[str]) -> Score:
    """Weighted sum over the user's priority order, with human-readable reasons."""
    total = 0.0
    reasons: list[str] = []
    for index, axis in enumerate(priorities):
        weight = PRIORITY_WEIGHTS[index] if index < len(PRIORITY_WEIGHTS) else 0.05
        value = axis_value(model, axis)
        total += weight * value
        if value >= 0.8:
            reasons.append(f"{axis}: stark")
        elif value <= 0.0:
            reasons.append(f"{axis}: fehlt")
        elif value <= UNKNOWN_FACTOR:
            reasons.append(f"{axis}: unbekannt")

    # A small nudge so a free model wins an otherwise exact tie, matching the cost policy.
    if model.is_free:
        total += 0.02
    if model.is_router:
        # Routers are a safety net rather than a first choice: a concrete model is preferred
        # when one is known to fit.
        total -= 0.15
        reasons.append("Router (Rückfallebene)")
    return Score(model=model, value=total, reasons=reasons)


def rank(models: list[ModelInfo], priorities: list[str], requirements: Requirements) -> list[Score]:
    """Eligible models, best first."""
    scored = [score_model(m, priorities) for m in models if meets(m, requirements)[0]]
    scored.sort(key=lambda s: s.value, reverse=True)
    return scored
