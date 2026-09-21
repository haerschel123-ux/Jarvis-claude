"""Memory pipeline (Spec §24, §25, §26).

    Conversation
      → candidate extraction
      → importance classification
      → duplicate detection
      → sensitive-data check
      → store / update / skip

Two things are deliberate:

* **Not every message becomes a memory.** Candidates come from a cheap rule pass first;
  a model is only asked when the rules found nothing and the mode allows it.
* **A credential never becomes a memory.** The sensitive check runs last and can veto
  anything the earlier stages proposed (Spec §25).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from core.config import Settings, get_settings
from core.enums import MemoryKind, MemoryMode
from core.events import EventType, event_bus
from core.logging_setup import get_logger
from memory import secrets_filter
from memory.database import Database, db
from memory.retrieval import MemoryRetrieval, retrieval, similarity

log = get_logger("memory")

UPDATE_THRESHOLD = 0.72     # above this, a candidate updates an existing memory
SKIP_THRESHOLD = 0.93       # above this, it is the same thing again — skip entirely


@dataclass(slots=True)
class MemoryCandidate:
    content: str
    subject: str = ""
    kind: MemoryKind = MemoryKind.FACT
    importance: float = 0.5
    confidence: float = 0.8
    source: str = "conversation"
    project_id: int | None = None
    conversation_id: int | None = None
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content, "subject": self.subject, "kind": self.kind.value,
            "importance": round(self.importance, 2), "confidence": round(self.confidence, 2),
        }


# --- rule-based extraction ------------------------------------------------------------------

# Each pattern captures the part of the sentence worth remembering. They are deliberately
# conservative: a missed memory is a minor loss, a wrong one pollutes every later prompt.
_RULES: list[tuple[re.Pattern[str], MemoryKind, float, str]] = [
    (re.compile(r"(?i)\bmerk(?:e)?\s+dir[,:]?\s+(.{6,300})"), MemoryKind.FACT, 0.95,
     "ausdrücklich zum Merken aufgefordert"),
    (re.compile(r"(?i)\b(?:bitte\s+)?behalte?\s+(?:im\s+kopf|dir)[,:]?\s+(.{6,300})"),
     MemoryKind.FACT, 0.9, "ausdrücklich zum Merken aufgefordert"),
    (re.compile(r"(?i)\bich\s+(?:bevorzuge|mag|nutze|verwende|arbeite\s+mit)\s+(.{4,200})"),
     MemoryKind.PREFERENCE, 0.72, "Vorliebe genannt"),
    (re.compile(r"(?i)\bich\s+(?:hasse|mag\s+kein|will\s+kein|m(?:ö|oe)chte\s+kein)\w*\s+(.{4,200})"),
     MemoryKind.PREFERENCE, 0.7, "Abneigung genannt"),
    (re.compile(r"(?i)\b(?:antworte|schreib|erkl(?:ä|ae)re|formatiere)\s+(?:mir\s+)?(?:immer|bitte\s+immer|grunds(?:ä|ae)tzlich)\s+(.{4,200})"),
     MemoryKind.PREFERENCE, 0.85, "dauerhafte Anweisung"),
    # A qualifier may sit between "mein" and the noun ("mein DayZ Server"), so one is allowed.
    (re.compile(r"(?i)\bmein\s+(?:[\w.-]+\s+){0,2}(?:projekt|repo|repository|server|bot|pc|rechner|editor)\s+"
                r"(?:hei(?:ß|ss)t|ist|liegt|l(?:ä|ae)uft)\s+(.{3,200})"),
     MemoryKind.PROJECT, 0.8, "Projektdetail genannt"),
    (re.compile(r"(?i)\b(?:das\s+)?projekt\s+([\w.\-]+)\s+(?:liegt|befindet\s+sich|ist)\s+(?:unter|in)\s+(.{3,160})"),
     MemoryKind.PROJECT, 0.78, "Projektpfad genannt"),
    # Both spellings, since many people type "heisse" without the sharp s.
    (re.compile(r"(?i)\bich\s+hei(?:ß|ss)e\s+([\w\s-]{2,60})"), MemoryKind.ENTITY, 0.9,
     "Name genannt"),
    (re.compile(r"(?i)\bimmer\s+wenn\s+(.{8,240})"), MemoryKind.WORKFLOW, 0.7,
     "wiederkehrende Arbeitsweise"),
]

# Wording that makes a statement momentary rather than lasting.
_EPHEMERAL = re.compile(
    r"(?i)\b(jetzt\s+gerade|gerade\s+eben|kurz|einmalig|heute\s+nur|mal\s+schnell|"
    r"zum\s+testen|nur\s+ein\s+test|vergiss\s+das\s+wieder)\b"
)


def extract_candidates(
    user_text: str,
    assistant_text: str = "",
    *,
    conversation_id: int | None = None,
    project_id: int | None = None,
) -> list[MemoryCandidate]:
    """Rule pass over one exchange. Only the user's own words are considered.

    The assistant's reply is not mined: remembering what JARVIS said would let a model
    hallucination become a durable "fact" about the user.
    """
    candidates: list[MemoryCandidate] = []
    text = (user_text or "").strip()
    if not text:
        return candidates

    for pattern, kind, importance, reason in _RULES:
        for match in pattern.finditer(text):
            captured = " ".join(part for part in match.groups() if part).strip(" .,:;!?")
            if len(captured) < 4:
                continue
            statement = _to_statement(match.group(0), captured)
            if _EPHEMERAL.search(statement):
                continue
            candidates.append(MemoryCandidate(
                content=statement,
                subject=_subject_for(captured),
                kind=kind,
                importance=importance,
                confidence=0.85,
                conversation_id=conversation_id,
                project_id=project_id,
                reasons=[reason],
            ))
            break   # one memory per rule per exchange is plenty

    return _deduplicate(candidates)


def _to_statement(full_match: str, captured: str) -> str:
    """Turn the matched phrase into a standalone statement about the user.

    German verbs are not rewritten from first to third person: doing that with regular
    expressions produces wrong conjugations ("Der Benutzer bevorzuge"). Instead the user's
    own wording is quoted verbatim behind a short attribution, which is always grammatical
    and also makes clear that this is what the user said rather than an inference.
    """
    cleaned = " ".join(full_match.split()).strip(" .,:;!?")
    lowered = cleaned.lower()

    if lowered.startswith(("merk dir", "merke dir", "behalte im kopf", "behalt im kopf",
                           "behalte dir", "behalt dir")):
        fact = re.sub(r"(?i)^dass\s+", "", captured.strip()).strip(" .,:;!?")
        if not fact:
            return ""
        # A "dass" clause is a subordinate clause; promoting it to a main clause would need
        # real grammar. When it still starts in the first person, the user's whole sentence
        # is kept instead — faithful, and correct German either way.
        if re.match(r"(?i)^(ich|mein|meine|mir|mich)\b", fact):
            return _sentence(cleaned[0].upper() + cleaned[1:])
        return _sentence(fact[0].upper() + fact[1:])

    if re.match(r"(?i)^ich\s+hei(ß|ss)e\b", cleaned):
        return f"Der Benutzer heißt {captured.strip(' .,:;!?')}."

    if lowered.startswith(("ich ", "mein ", "meine ")):
        return f'Der Benutzer sagt über sich: "{_sentence(cleaned)}"'

    return _sentence(cleaned)


def _sentence(text: str) -> str:
    text = text.strip()
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else text + "."


def _subject_for(text: str) -> str:
    words = [w for w in re.split(r"\s+", text) if w]
    return " ".join(words[:4])[:60]


def _deduplicate(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    unique: list[MemoryCandidate] = []
    for candidate in candidates:
        if any(similarity(candidate.content, other.content) > 0.8 for other in unique):
            continue
        unique.append(candidate)
    return unique


def parse_model_candidates(raw: str) -> list[MemoryCandidate]:
    """Parse the extraction model's JSON answer, tolerating surrounding prose."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    if not match:
        return []
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []

    result: list[MemoryCandidate] = []
    for entry in payload.get("memories") or []:
        if not isinstance(entry, dict):
            continue
        content = str(entry.get("content") or "").strip()
        if len(content) < 6:
            continue
        try:
            kind = MemoryKind(str(entry.get("kind", "fact")).lower())
        except ValueError:
            kind = MemoryKind.FACT
        try:
            importance = min(max(float(entry.get("importance", 0.5)), 0.0), 1.0)
        except (TypeError, ValueError):
            importance = 0.5
        result.append(MemoryCandidate(
            content=content,
            subject=str(entry.get("subject") or _subject_for(content))[:60],
            kind=kind,
            importance=importance,
            confidence=0.7,          # a model's judgement is trusted less than an explicit rule
            source="model",
            reasons=["vom Modell vorgeschlagen"],
        ))
    return _deduplicate(result)


# --- the manager -----------------------------------------------------------------------------


class MemoryManager:
    def __init__(
        self,
        database: Database | None = None,
        retrieval_engine: MemoryRetrieval | None = None,
    ) -> None:
        self._db = database or db
        self._retrieval = retrieval_engine or retrieval

    async def consider(
        self, candidate: MemoryCandidate, settings: Settings | None = None
    ) -> dict[str, Any]:
        """Run one candidate through the remaining pipeline stages."""
        settings = settings or get_settings()
        mode = settings.memory.mode

        if mode is MemoryMode.OFF:
            return {"action": "skip", "reason": "Gedächtnis ist ausgeschaltet"}
        if mode is MemoryMode.IMPORTANT_ONLY and candidate.importance < 0.75:
            return {"action": "skip", "reason": "nicht wichtig genug für diesen Modus"}
        if candidate.importance < settings.memory.importance_threshold:
            return {"action": "skip", "reason": "unter der Wichtigkeitsschwelle"}

        # The sensitive check comes last and overrides everything above it (Spec §25).
        if settings.memory.secret_filter_enabled:
            verdict = secrets_filter.check(candidate.content, candidate.subject)
            if not verdict.allowed:
                log.info("Gedächtnis-Kandidat abgelehnt: %s", verdict.reason)
                return {"action": "blocked", "reason": verdict.reason}

        existing = await self._retrieval.find_similar(
            candidate.subject, candidate.content, threshold=UPDATE_THRESHOLD
        )
        if existing is not None:
            score = similarity(candidate.content, existing["content"])
            if score >= SKIP_THRESHOLD:
                return {"action": "skip", "reason": "bereits gespeichert", "id": existing["id"]}
            updated = await self.update(
                existing["id"],
                content=candidate.content,
                importance=max(candidate.importance, float(existing["importance"])),
            )
            return {"action": "update", "id": existing["id"], "memory": updated}

        memory_id = await self.store(candidate)
        return {"action": "create", "id": memory_id}

    async def store(self, candidate: MemoryCandidate) -> int:
        memory_id = await self._db.execute(
            """
            INSERT INTO memories
                (kind, subject, content, importance, confidence, source, project_id,
                 conversation_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.kind.value, candidate.subject, candidate.content,
                candidate.importance, candidate.confidence, candidate.source,
                candidate.project_id, candidate.conversation_id,
            ),
        )
        log.info("Gemerkt [%s] %s", candidate.kind.value, candidate.subject or candidate.content[:60])
        event_bus.emit(EventType.MEMORY_CREATED, id=memory_id, subject=candidate.subject,
                       kind=candidate.kind.value)
        return memory_id

    async def update(self, memory_id: int, **fields: Any) -> dict[str, Any] | None:
        allowed = {"content", "subject", "kind", "importance", "confidence", "pinned", "disabled"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return await self._retrieval.by_id(memory_id)

        # A memory is long-lived; re-check the edited content rather than trusting the caller.
        if "content" in updates or "subject" in updates:
            current = await self._retrieval.by_id(memory_id) or {}
            verdict = secrets_filter.check(
                str(updates.get("content", current.get("content", ""))),
                str(updates.get("subject", current.get("subject", ""))),
            )
            if not verdict.allowed:
                raise ValueError(f"Inhalt abgelehnt: {verdict.reason}")

        assignments = ", ".join(f"{key} = ?" for key in updates)
        await self._db.execute(
            f"UPDATE memories SET {assignments}, updated_at = datetime('now') WHERE id = ?",
            [*updates.values(), memory_id],
        )
        event_bus.emit(EventType.MEMORY_UPDATED, id=memory_id)
        return await self._retrieval.by_id(memory_id)

    async def delete(self, memory_id: int) -> bool:
        existing = await self._retrieval.by_id(memory_id)
        if existing is None:
            return False
        await self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        event_bus.emit(EventType.MEMORY_DELETED, id=memory_id)
        return True

    async def capture_exchange(
        self,
        user_text: str,
        assistant_text: str = "",
        *,
        conversation_id: int | None = None,
        project_id: int | None = None,
        settings: Settings | None = None,
    ) -> list[dict[str, Any]]:
        """Run the full pipeline over one exchange. Never raises into the chat path."""
        settings = settings or get_settings()
        if settings.memory.mode is MemoryMode.OFF:
            return []
        results: list[dict[str, Any]] = []
        try:
            candidates = extract_candidates(
                user_text, assistant_text,
                conversation_id=conversation_id, project_id=project_id,
            )
            for candidate in candidates:
                results.append(await self.consider(candidate, settings))
        except Exception:
            log.exception("Gedächtnis-Erfassung fehlgeschlagen")
        return results

    async def recall(self, query: str, limit: int = 12) -> list[dict[str, Any]]:
        return await self._retrieval.search(query, limit)

    async def list(
        self,
        *,
        query: str = "",
        kind: str | None = None,
        limit: int = 100,
        include_disabled: bool = True,
    ) -> list[dict[str, Any]]:
        if query:
            return await self._retrieval.search(
                query, limit, kinds=[kind] if kind else None, include_disabled=include_disabled
            )
        conditions = []
        params: list[Any] = []
        if kind:
            conditions.append("kind = ?")
            params.append(kind)
        if not include_disabled:
            conditions.append("disabled = 0")
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        return await self._db.fetch_all(
            f"SELECT * FROM memories {where} ORDER BY pinned DESC, importance DESC, id DESC LIMIT ?",
            params,
        )

    async def export(self) -> dict[str, Any]:
        """Export for backup. Secrets were never stored, so nothing has to be stripped."""
        rows = await self._db.fetch_all("SELECT * FROM memories ORDER BY id")
        return {"version": 1, "count": len(rows), "memories": rows}

    async def import_(self, payload: dict[str, Any], settings: Settings | None = None) -> dict[str, int]:
        """Import memories, running each through the sensitive check again."""
        settings = settings or get_settings()
        created = skipped = blocked = 0
        for entry in payload.get("memories") or []:
            content = str(entry.get("content") or "").strip()
            if not content:
                skipped += 1
                continue
            subject = str(entry.get("subject") or "")
            if settings.memory.secret_filter_enabled and not secrets_filter.is_safe(content, subject):
                blocked += 1
                continue
            try:
                kind = MemoryKind(str(entry.get("kind", "fact")).lower())
            except ValueError:
                kind = MemoryKind.FACT
            if await self._retrieval.find_similar(subject, content, SKIP_THRESHOLD):
                skipped += 1
                continue
            await self.store(MemoryCandidate(
                content=content, subject=subject, kind=kind,
                importance=float(entry.get("importance") or 0.5),
                confidence=float(entry.get("confidence") or 0.8),
                source="import",
            ))
            created += 1
        return {"created": created, "skipped": skipped, "blocked": blocked}

    async def counts(self) -> dict[str, int]:
        return await self._retrieval.counts()


memory_manager = MemoryManager()
