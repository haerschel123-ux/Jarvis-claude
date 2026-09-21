"""Coordinator (Spec §30, §31, §32).

Decides how much machinery a request deserves:

    answer directly  →  one tool  →  one specialist  →  a full multi-agent workflow

The specification is explicit that five agents must not start for every little thing, so the
escalation decision is a **rule-based, testable function** rather than a model call. Starting
a planner to decide whether to start a planner would be absurd — and slow, and not free.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from core.config import Settings
from core.enums import Intent
from core.events import EventType, event_bus
from core.intent import IntentResult
from core.logging_setup import get_logger

log = get_logger("agents.coordinator")


class Strategy(IntEnum):
    """How much to spend on a request, cheapest first."""

    DIRECT = 0        # answer from the model alone
    SINGLE_TOOL = 1   # one tool call, then answer
    SPECIALIST = 2    # hand to one specialist agent
    WORKFLOW = 3      # planner → executor → reviewer


@dataclass
class Decision:
    strategy: Strategy
    agents: list[str] = field(default_factory=list)
    reason: str = ""
    review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy.name,
            "agents": self.agents,
            "reason": self.reason,
            "review": self.review,
        }


# Intents that always belong to a specialist, and which one.
SPECIALIST_FOR: dict[Intent, str] = {
    Intent.WEB_RESEARCH: "research",
    Intent.CODING_TASK: "coding",
    Intent.SCREEN_ACTION: "computer",
    Intent.DAYZ_ACTION: "dayz",
    Intent.DISCORD_ACTION: "discord",
    Intent.GITHUB_ACTION: "github",
}

# Intents that are a single tool call in the overwhelming majority of cases.
SINGLE_TOOL_INTENTS = frozenset({
    Intent.REMINDER, Intent.MEMORY_ACTION, Intent.SYSTEM_ACTION,
    Intent.CALENDAR_ACTION, Intent.EMAIL_ACTION, Intent.SMART_HOME_ACTION,
    Intent.FILE_TASK,
})

# Wording that signals a genuinely multi-step job.
_MULTI_STEP = re.compile(
    r"(?i)("
    r"\bund\s+dann\b|\bdanach\b|\banschließend\b|\bnacheinander\b|"
    r"\bschritt\s+für\s+schritt\b|\berst\b.{0,60}\bdann\b|"
    r"\bkomplett(?:e|es|en)?\b.{0,30}(?:überarbeit|analysier|durchgeh|prüf)|"
    r"\ball(?:e|es)\b.{0,25}(?:fehler|dateien|tests)\b.{0,25}(?:repariere|behebe|beheb)|"
    r"\brefactor\w*\b|\bmigriere\b|\bportiere\b"
    r")"
)

# Wording that marks a task as consequential enough for an independent second opinion.
_CRITICAL = re.compile(
    r"(?i)("
    r"\bproduktiv\w*\b|\bproduction\b|\blive[-\s]?server\b|"
    r"\bmigration\b|\bdatenbank\w*\s+(?:ändern|migrieren|löschen)|"
    r"\bsicherheit\w*\b|\bauthentifizier\w*\b|\bverschlüssel\w*\b|"
    r"\blösche?\b.{0,25}\balle\b|\bdeploy\w*\b"
    r")"
)

# Very short, clearly conversational messages never escalate.
TRIVIAL_LENGTH = 24

# "Öffne Discord", "starte VS Code": a domain word in the sentence does not make it domain
# work. These are one tool call, whatever the intent classifier labelled them.
_SIMPLE_LAUNCH = re.compile(
    r"(?i)^\s*(?:jarvis[,\s]+)?(?:bitte\s+)?"
    r"(?:öffne|oeffne|starte|start|open|schließe|schliesse|beende|close)\s+"
    r"[\w\s.+-]{2,40}[.!?]?\s*$"
)


class Coordinator:
    """Chooses the strategy and, for workflows, the agent sequence."""

    def decide(
        self,
        message: str,
        intent: IntentResult,
        settings: Settings,
        *,
        has_tools: bool = True,
    ) -> Decision:
        text = (message or "").strip()
        kind = intent.intent

        multi_step = bool(_MULTI_STEP.search(text)) or kind is Intent.MULTI_STEP_TASK
        critical = bool(_CRITICAL.search(text))

        # 1. Small talk and short questions: never escalate (Spec §30).
        #    A message the classifier was unsure about still escalates when it clearly
        #    describes multi-step or consequential work — "conversation" must not become a
        #    way around the safeguards.
        if not multi_step and not critical and (
            kind is Intent.CONVERSATION
            or (len(text) < TRIVIAL_LENGTH and kind is Intent.QUESTION)
        ):
            return Decision(Strategy.DIRECT, reason="Unterhaltung — kein Agent nötig")

        # 2. A knowledge question only needs research when it is actually time-sensitive;
        #    the intent classifier already separates those.
        if kind is Intent.QUESTION and not multi_step and not critical:
            return Decision(Strategy.DIRECT, reason="Wissensfrage ohne Werkzeugbedarf")

        if not has_tools:
            return Decision(Strategy.DIRECT, reason="Keine Werkzeuge verfügbar")

        review = critical and settings.models.multi_agent_review

        # 3. Starting or closing a program is one tool call, even when the program name
        #    happens to be a domain the assistant also has a specialist for.
        if _SIMPLE_LAUNCH.match(text):
            return Decision(Strategy.SINGLE_TOOL, reason="Programm starten oder schließen")

        # 4. A genuinely multi-step job gets the full workflow.
        if multi_step:
            agents = ["planner", "coding" if kind is Intent.CODING_TASK else "executor"]
            if settings.models.multi_agent_review or critical:
                agents.append("review")
            return Decision(
                Strategy.WORKFLOW, agents,
                reason="Mehrstufige Aufgabe — Planer, Ausführung und Prüfung",
                review=review,
            )

        # 5. Domain work goes to its specialist.
        if specialist := SPECIALIST_FOR.get(kind):
            agents = [specialist]
            if review:
                agents.append("review")
            return Decision(
                Strategy.SPECIALIST, agents,
                reason=f"Fachaufgabe — {specialist}-Agent",
                review=review,
            )

        # 6. Everything else is one tool call.
        if kind in SINGLE_TOOL_INTENTS:
            return Decision(Strategy.SINGLE_TOOL, reason="Eine Werkzeugaktion genügt")

        # 7. Consequential work that no rule classified still gets a plan and a review
        #    rather than a single unchecked shot.
        if critical:
            agents = ["planner", "executor", "review"]
            return Decision(
                Strategy.WORKFLOW, agents,
                reason="Folgenreiche Aufgabe — mit Plan und Prüfung",
                review=review,
            )

        return Decision(Strategy.DIRECT, reason="Direkt beantwortbar")

    def explain(self, decision: Decision) -> str:
        """A short, user-facing justification (Spec §117: "why this action?")."""
        if decision.strategy is Strategy.DIRECT:
            return "Ich beantworte das direkt."
        if decision.strategy is Strategy.SINGLE_TOOL:
            return "Dafür genügt ein Werkzeugaufruf."
        if decision.strategy is Strategy.SPECIALIST:
            return f"Ich übergebe das an den {decision.agents[0]}-Agenten."
        names = " → ".join(decision.agents)
        return f"Das ist mehrstufig, deshalb: {names}."

    def announce(self, decision: Decision) -> None:
        event_bus.emit(EventType.AGENT_STATUS, agent="coordinator", **decision.to_dict())
        log.info("Koordinator: %s (%s)", decision.strategy.name, decision.reason)


coordinator = Coordinator()
