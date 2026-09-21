"""Memory retrieval (Spec §24, §65).

Full-text search over SQLite FTS5, re-ranked by importance, recency and how often a memory
has actually been useful. No embeddings and no paid API: the core must not depend on one
(Spec §24), and for a personal assistant's memory volume, FTS with good ranking is enough.
"""

from __future__ import annotations

import re
from typing import Any

from core.logging_setup import get_logger
from memory.database import Database, db

log = get_logger("memory.retrieval")

# Words too common to narrow a search; dropping them stops a query like "wie heisst mein
# Projekt" from matching everything.
_STOPWORDS = frozenset({
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einen", "einem", "eines",
    "und", "oder", "aber", "ist", "sind", "war", "waren", "bin", "bist", "sein", "hat",
    "habe", "haben", "wie", "was", "wer", "wo", "wann", "warum", "wieso", "welche",
    "welcher", "welches", "mein", "meine", "meinen", "meiner", "dein", "deine", "ich",
    "du", "er", "sie", "es", "wir", "ihr", "mir", "mich", "dir", "dich", "für", "von",
    "mit", "auf", "in", "an", "zu", "bei", "aus", "nach", "über", "unter", "vor",
    "the", "a", "and", "or", "is", "are", "my", "your", "what", "how",
})

_WORD = re.compile(r"[\wäöüßÄÖÜ][\wäöüß'-]*", re.UNICODE)

MIN_TERM_LENGTH = 3


def build_match_query(text: str, limit_terms: int = 8) -> str:
    """Turn free text into a safe FTS5 MATCH expression.

    Terms are quoted, so user input can never be interpreted as FTS syntax, and joined with
    OR plus a prefix wildcard so German compounds and inflections still match.
    """
    terms: list[str] = []
    for word in _WORD.findall(text or ""):
        lowered = word.lower()
        if len(lowered) < MIN_TERM_LENGTH or lowered in _STOPWORDS:
            continue
        # Double the quote to escape it inside an FTS5 string literal.
        safe = lowered.replace('"', '""')
        terms.append(f'"{safe}"*')
        if len(terms) >= limit_terms:
            break
    return " OR ".join(terms)


class MemoryRetrieval:
    def __init__(self, database: Database | None = None) -> None:
        self._db = database or db

    async def search(
        self,
        query: str,
        limit: int = 12,
        *,
        kinds: list[str] | None = None,
        project_id: int | None = None,
        include_disabled: bool = False,
    ) -> list[dict[str, Any]]:
        """Relevance-ranked search. Pinned memories always come first."""
        match = build_match_query(query)
        rows: list[dict[str, Any]] = []

        if match:
            conditions = ["m.disabled = 0"] if not include_disabled else []
            params: list[Any] = [match]
            if kinds:
                conditions.append(f"m.kind IN ({','.join('?' * len(kinds))})")
                params.extend(kinds)
            if project_id is not None:
                conditions.append("(m.project_id IS NULL OR m.project_id = ?)")
                params.append(project_id)
            where = (" AND " + " AND ".join(conditions)) if conditions else ""
            params.append(limit * 3)

            rows = await self._db.fetch_all(
                f"""
                SELECT m.*, bm25(memories_fts) AS rank
                  FROM memories_fts
                  JOIN memories m ON m.id = memories_fts.rowid
                 WHERE memories_fts MATCH ?{where}
                 ORDER BY rank
                 LIMIT ?
                """,
                params,
            )

        # Pinned memories are context the user explicitly asked to always have available.
        pinned = await self.pinned(limit=6, project_id=project_id)
        rows = pinned + [row for row in rows if row["id"] not in {p["id"] for p in pinned}]

        scored = sorted(rows, key=self._score, reverse=True)[:limit]
        if scored:
            await self._touch([row["id"] for row in scored])
        return scored

    @staticmethod
    def _score(row: dict[str, Any]) -> float:
        """Combine text relevance with importance, usefulness and recency."""
        # bm25 returns a negative number where lower is better, so it is inverted here.
        relevance = -float(row.get("rank") or 0.0)
        importance = float(row.get("importance") or 0.5)
        hits = min(int(row.get("hits") or 0), 20) / 20.0
        pinned = 5.0 if row.get("pinned") else 0.0
        confidence = float(row.get("confidence") or 0.8)
        return pinned + relevance * 0.5 + importance * 2.0 + hits * 0.6 + confidence * 0.4

    async def _touch(self, ids: list[int]) -> None:
        """Record that these memories were used, which feeds the ranking next time."""
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        await self._db.execute(
            f"UPDATE memories SET hits = hits + 1, last_used_at = datetime('now') "
            f"WHERE id IN ({placeholders})",
            ids,
        )

    async def pinned(self, limit: int = 10, project_id: int | None = None) -> list[dict[str, Any]]:
        params: list[Any] = []
        clause = ""
        if project_id is not None:
            clause = " AND (project_id IS NULL OR project_id = ?)"
            params.append(project_id)
        params.append(limit)
        return await self._db.fetch_all(
            f"SELECT *, 0 AS rank FROM memories WHERE pinned = 1 AND disabled = 0{clause} "
            f"ORDER BY importance DESC LIMIT ?",
            params,
        )

    async def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM memories WHERE disabled = 0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def by_id(self, memory_id: int) -> dict[str, Any] | None:
        return await self._db.fetch_one("SELECT * FROM memories WHERE id = ?", (memory_id,))

    async def find_similar(
        self, subject: str, content: str, threshold: float = 0.72
    ) -> dict[str, Any] | None:
        """Find an existing memory close enough to be an update rather than a new entry."""
        match = build_match_query(f"{subject} {content}", limit_terms=6)
        if not match:
            return None
        candidates = await self._db.fetch_all(
            """
            SELECT m.* FROM memories_fts
              JOIN memories m ON m.id = memories_fts.rowid
             WHERE memories_fts MATCH ? LIMIT 20
            """,
            (match,),
        )
        best: dict[str, Any] | None = None
        best_score = 0.0
        for candidate in candidates:
            score = similarity(content, candidate["content"])
            subject_score = similarity(subject, candidate.get("subject") or "")
            combined = max(score, (score * 0.7 + subject_score * 0.3))
            if combined > best_score:
                best, best_score = candidate, combined
        return best if best_score >= threshold else None

    async def counts(self) -> dict[str, int]:
        rows = await self._db.fetch_all(
            "SELECT kind, COUNT(*) AS total FROM memories WHERE disabled = 0 GROUP BY kind"
        )
        result = {row["kind"]: int(row["total"]) for row in rows}
        result["total"] = sum(result.values())
        return result


def tokenise(text: str) -> set[str]:
    return {
        word.lower() for word in _WORD.findall(text or "")
        if len(word) >= MIN_TERM_LENGTH and word.lower() not in _STOPWORDS
    }


def similarity(left: str, right: str) -> float:
    """Jaccard similarity over content words. Cheap, language-agnostic, good enough here."""
    a, b = tokenise(left), tokenise(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


retrieval = MemoryRetrieval()
