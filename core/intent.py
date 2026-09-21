"""Intent classification (Spec §13).

Two stages, in this order:

1. **Rules.** German and English patterns cover the commands a personal assistant actually
   receives. They are instant, free, work offline and — importantly — are deterministic, so
   "Jarvis, öffne Discord" never depends on a model being reachable.
2. **Model.** Only when the rules are not confident enough, and only with a fast, cheap model.

A wrong-but-confident classification is worse than an unsure one, so the rules return a
confidence and the caller decides whether that is good enough.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.enums import Intent
from core.logging_setup import get_logger

log = get_logger("intent")

CONFIDENT = 0.75
UNSURE = 0.45


@dataclass(slots=True)
class IntentResult:
    intent: Intent
    confidence: float
    matched: list[str] = field(default_factory=list)
    method: str = "rules"

    @property
    def is_confident(self) -> bool:
        return self.confidence >= CONFIDENT

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent": self.intent.value,
            "confidence": round(self.confidence, 2),
            "matched": self.matched,
            "method": self.method,
        }


def _p(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


# Ordered most-specific first: the first rule set that matches wins, so "prüfe meinen DayZ
# Server" is a DAYZ_ACTION rather than a generic SYSTEM_ACTION.
RULES: list[tuple[Intent, list[re.Pattern[str]], float]] = [
    (Intent.DAYZ_ACTION, [
        _p(r"\bdayz\b"), _p(r"\bnitrado\b"), _p(r"\btypes\.xml\b"), _p(r"\bevents\.xml\b"),
        _p(r"\bcfg(?:eventspawns|spawnabletypes|gameplay|economycore|randompresets)\b"),
        _p(r"\brpt[- ]?log"), _p(r"\b(chernarus|livonia|enoch|sakhal)\b"),
        _p(r"\b(?:spawn|loot)(?:punkte|points?|s)?\b.*\b(?:server|map)\b"),
    ], 0.9),
    (Intent.DISCORD_ACTION, [
        _p(r"\bdiscord\b"), _p(r"\bdiscord\.py\b"), _p(r"\bslash[- ]commands?\b"),
        _p(r"\bmein(?:en)? bot\b"), _p(r"\bbot[- ](?:status|logs?|prozess)\b"),
    ], 0.85),
    (Intent.GITHUB_ACTION, [
        _p(r"\bgithub\b"), _p(r"\bpull[- ]request\b"), _p(r"\brepositor(?:y|ies|ium|ien)\b"),
        _p(r"\bgit (?:status|diff|commit|push|pull|log|branch)\b"), _p(r"\bcommitte?\b"),
        _p(r"\bissues?\b.*\b(?:github|repo)\b"),
    ], 0.85),
    (Intent.SMART_HOME_ACTION, [
        _p(r"\b(?:licht|lampe|lampen|steckdose|heizung|thermostat|rollladen|szene)\b"),
        _p(r"\bhome[- ]?assistant\b"), _p(r"\b(?:schalte|mach)\b.*\b(?:ein|aus|an)\b"),
        _p(r"\btemperatur\b.*\b(?:wohnzimmer|zimmer|raum|draußen)\b"),
    ], 0.8),
    (Intent.CALENDAR_ACTION, [
        _p(r"\bkalender\b"), _p(r"\btermin\b"), _p(r"\bmeeting\b"),
        _p(r"\bwas (?:steht|ist) (?:heute|morgen|diese woche)\b"),
        _p(r"\btrag(?:e)?\b.*\b(?:ein|hinzu)\b.*\b(?:uhr|termin)\b"),
    ], 0.85),
    (Intent.EMAIL_ACTION, [
        _p(r"\be-?mails?\b"), _p(r"\bgmail\b"), _p(r"\bposteingang\b"), _p(r"\bmailbox\b"),
        _p(r"\b(?:schreib|sende|antworte)\b.*\bmail\b"),
    ], 0.85),
    (Intent.REMINDER, [
        _p(r"\berinnere?\s+mich\b"), _p(r"\berinnerung\b"), _p(r"\bremind\s+me\b"),
        _p(r"\bjeden\s+(?:tag|montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)\b"),
        _p(r"\bin\s+\d+\s+(?:minuten?|stunden?|tagen?)\b.*\b(?:sag|erinner|melde)\b"),
        _p(r"\bwecke?\s+mich\b"),
    ], 0.9),
    (Intent.MEMORY_ACTION, [
        _p(r"\bmerk(?:e)?\s+dir\b"), _p(r"\bmerk(?:e)?\s+dich\b"), _p(r"\bbehalte?\b.*\bim\s+kopf\b"),
        _p(r"\bvergiss\b"), _p(r"\bwas\s+weißt\s+du\s+über\s+mich\b"),
        _p(r"\b(?:speicher|notier)(?:e)?\b.*\b(?:dauerhaft|langfristig)\b"),
        _p(r"\bdein\s+gedächtnis\b"), _p(r"\berinnerst\s+du\s+dich\b"),
    ], 0.85),
    (Intent.SCREEN_ACTION, [
        _p(r"\bbildschirm\b"), _p(r"\bscreenshot\b"), _p(r"\bwas\s+siehst\s+du\b"),
        _p(r"\bschau\s+(?:dir|mal)\b.*\b(?:an|bildschirm)\b"), _p(r"\bklicke?\b"),
        _p(r"\b(?:maus|mauszeiger)\b"), _p(r"\btipp(?:e)?\b.*\bein\b"),
        _p(r"\bfenster\b.*\b(?:fokus|aktiv|wechsl)"),
    ], 0.85),
    (Intent.SYSTEM_ACTION, [
        _p(r"\b(?:öffne|oeffne|starte|schlie(?:ß|ss)e|beende|start|open|close)\b\s+\S"),
        _p(r"\bprogramm\b"), _p(r"\banwendung\b"), _p(r"\bfahre?\b.*\bherunter\b"),
        _p(r"\bprozess(?:e)?\b"), _p(r"\btask[- ]?manager\b"),
    ], 0.8),
    (Intent.CODING_TASK, [
        _p(r"\bcode\b"), _p(r"\bfunktion\b"), _p(r"\bklasse\b"), _p(r"\bbug\b"),
        _p(r"\bfehler\b.*\b(?:code|script|programm|projekt)\b"), _p(r"\btraceback\b"),
        _p(r"\bimplementier\b"), _p(r"\brefactor\b"), _p(r"\bunit[- ]?test\b"),
        _p(r"\b(?:python|javascript|typescript|rust|java|c\+\+|sql)\b"),
        _p(r"\bschreib(?:e)?\b.*\b(?:skript|script|programm|funktion)\b"),
        _p(r"\brepariere?\b.*\b(?:fehler|bug|code)\b"),
        _p(r"\b(?:migriere|migration|deploye?|deployment|rollout)\b"),
        _p(r"\b(?:datenbank|schema|api)\b.*\b(?:ändern|anpassen|migrieren|erweitern)\b"),
    ], 0.8),
    (Intent.FILE_TASK, [
        _p(r"\bdatei(?:en)?\b"), _p(r"\bordner\b"), _p(r"\bverzeichnis\b"),
        _p(r"\b(?:lies|lese|öffne|kopiere?|verschiebe?|lösche?|erstelle?)\b.*\b(?:datei|ordner|\.\w{2,4}\b)"),
        _p(r"[A-Za-z]:\\"), _p(r"\bsuche?\b.*\b(?:datei|ordner|im\s+verzeichnis)\b"),
    ], 0.8),
    (Intent.WEB_RESEARCH, [
        _p(r"\b(?:suche?|recherchier(?:e)?|google)\b.*\b(?:im\s+)?(?:internet|web|netz|online)\b"),
        _p(r"\bim\s+internet\b"), _p(r"\bsuch(?:e)?\s+nach\b"),
        _p(r"\baktuelle[rsn]?\b.*\b(?:preis|version|nachrichten|stand|wetter)\b"),
        _p(r"\bwie\s+viel\s+kostet\b"), _p(r"\bneueste\s+version\b"),
        _p(r"\bwas\s+gibt(?:'|)s\s+neues\b"),
    ], 0.85),
    (Intent.MULTI_STEP_TASK, [
        _p(r"\bund\s+dann\b.*\bund\b"), _p(r"\bnacheinander\b"), _p(r"\bschritt\s+für\s+schritt\b"),
        _p(r"\berst\b.*\bdann\b.*\b(?:zum\s+schluss|danach|anschließend)\b"),
        _p(r"\bkomplett(?:e|es)?\b.*\b(?:durchgehen|analysier|überarbeit)"),
    ], 0.7),
    (Intent.QUESTION, [
        _p(r"^\s*(?:was|wer|wie|wo|wann|warum|wieso|welche[rsn]?|kannst du|weißt du)\b"),
        _p(r"\?\s*$"),
    ], 0.6),
]

# Short social phrases that should never trigger a tool or an agent.
SMALL_TALK = [
    _p(r"^\s*(?:hallo|hi|hey|moin|servus|guten\s+(?:morgen|tag|abend)|jarvis)[\s!,.?]*$"),
    _p(r"^\s*(?:danke|dankeschön|vielen\s+dank|passt|ok(?:ay)?|alles\s+klar)[\s!,.?]*$"),
    _p(r"^\s*(?:tschüss|bis\s+später|gute\s+nacht|bye)[\s!,.?]*$"),
    _p(r"^\s*wie\s+geht(?:'|)s(?:\s+dir)?[\s!,.?]*$"),
]


def classify_rules(text: str) -> IntentResult:
    """Deterministic first pass. Never raises, never needs the network."""
    stripped = (text or "").strip()
    if not stripped:
        return IntentResult(Intent.CONVERSATION, 1.0, method="empty")

    for pattern in SMALL_TALK:
        if pattern.search(stripped):
            return IntentResult(Intent.CONVERSATION, 0.95, [pattern.pattern], "rules")

    best: IntentResult | None = None
    for intent, patterns, base_confidence in RULES:
        matched = [p.pattern for p in patterns if p.search(stripped)]
        if not matched:
            continue
        # Several independent signals for the same intent raise confidence, but never to 1.0 —
        # rules are evidence, not proof.
        confidence = min(base_confidence + 0.05 * (len(matched) - 1), 0.97)
        if best is None or confidence > best.confidence:
            best = IntentResult(intent, confidence, matched, "rules")

    if best is not None:
        return best
    # A long message with no signal is a conversation; a short one is too, just less certainly.
    confidence = 0.6 if len(stripped) > 80 else 0.5
    return IntentResult(Intent.CONVERSATION, confidence, method="rules")


def parse_model_classification(raw: str) -> IntentResult | None:
    """Parse the classifier model's JSON answer, tolerating surrounding prose."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return None
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    name = str(payload.get("intent", "")).strip().upper()
    try:
        intent = Intent(name)
    except ValueError:
        return None
    try:
        confidence = float(payload.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7
    return IntentResult(intent, min(max(confidence, 0.0), 1.0), method="model")


def category_listing() -> str:
    """The category block injected into the classifier prompt."""
    descriptions = {
        Intent.CONVERSATION: "Small Talk, Begrüßung, Meinungsaustausch ohne Aufgabe",
        Intent.QUESTION: "Wissensfrage, die ohne Werkzeuge beantwortbar ist",
        Intent.WEB_RESEARCH: "Braucht aktuelle Informationen aus dem Internet",
        Intent.FILE_TASK: "Dateien oder Ordner lesen, schreiben, suchen, verschieben",
        Intent.CODING_TASK: "Code verstehen, schreiben, ändern, testen, Fehler beheben",
        Intent.SYSTEM_ACTION: "Programme starten oder schließen, Prozesse, Systeminfos",
        Intent.SCREEN_ACTION: "Bildschirm ansehen, Maus, Tastatur, Fenster steuern",
        Intent.MEMORY_ACTION: "Etwas dauerhaft merken, vergessen oder abrufen",
        Intent.REMINDER: "Erinnerung oder geplante Aufgabe anlegen",
        Intent.CALENDAR_ACTION: "Kalendertermine lesen oder anlegen",
        Intent.EMAIL_ACTION: "E-Mails lesen, zusammenfassen, beantworten, senden",
        Intent.DISCORD_ACTION: "Discord-Bot-Projekt, Logs, Prozess, Status",
        Intent.GITHUB_ACTION: "GitHub oder Git: Repositories, Commits, Branches, PRs",
        Intent.DAYZ_ACTION: "DayZ-Server, Nitrado, XML/JSON-Konfiguration, RPT-Logs",
        Intent.SMART_HOME_ACTION: "Licht, Schalter, Sensoren, Szenen, Home Assistant",
        Intent.MULTI_STEP_TASK: "Mehrere zusammenhängende Schritte, braucht einen Plan",
    }
    return "\n".join(f"- {intent.value}: {text}" for intent, text in descriptions.items())
