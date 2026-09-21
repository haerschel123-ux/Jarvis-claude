"""Review agent (Spec §31, §32).

Checks another agent's work independently. The router is asked for a *different* model where
one is available, because a second opinion from the same model is mostly an echo.

The reviewer is deliberately read-only: its job is to find problems, not to fix them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agents.base import Agent, AgentContext, AgentResult
from core.logging_setup import get_logger

log = get_logger("agents.review")

SYSTEM = """Du bist der Prüf-Agent von {name}. Du prüfst die Arbeit eines anderen Agenten.

Prüfe in dieser Reihenfolge:
1. Korrektheit — tut es wirklich, was verlangt war?
2. Vollständigkeit — fehlt ein Teil der Aufgabe?
3. Nebenwirkungen — wird etwas kaputtgemacht, das vorher funktionierte?
4. Sicherheit — Zugangsdaten, Pfade außerhalb der Freigabe, zerstörende Aktionen?
5. Ehrlichkeit — wird etwas behauptet, das gar nicht ausgeführt wurde?

Regeln:
- Du änderst nichts. Du prüfst und benennst.
- Sei konkret: nenne Datei, Stelle und das tatsächliche Problem.
- Erfinde keine Probleme. Wenn es gut ist, sage das in einem Satz.
- Unterscheide zwischen "muss behoben werden" und "wäre schöner".

Antworte ausschließlich mit JSON:
{{"verdict": "ok" | "needs_changes" | "rejected",
  "summary": "ein bis zwei Sätze",
  "issues": [{{"severity": "blocker" | "major" | "minor",
               "where": "Datei oder Stelle", "problem": "was falsch ist",
               "suggestion": "wie es zu beheben wäre"}}]}}"""


class ReviewAgent(Agent):
    name = "review"
    role = "Prüft die Arbeit eines anderen Agenten unabhängig"
    task_kind = "review"
    needs_tools = True
    tool_tags = {"files", "git"}
    max_output_tokens = 1400

    def __init__(self, *, avoid_model: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._avoid_model = avoid_model

    def system_prompt(self, context: AgentContext) -> str:
        return SYSTEM.format(name=context.settings.assistant.name)

    def user_prompt(self, context: AgentContext) -> str:
        parts = [f"Ursprüngliche Aufgabe:\n{context.goal}"]
        if plan := context.inputs.get("plan"):
            parts.append(f"\nGeplant war:\n{plan}")
        if work := context.inputs.get("work"):
            parts.append(f"\nDas wurde berichtet:\n{work}")
        if files := context.inputs.get("changed_files"):
            parts.append(f"\nGeänderte Dateien laut Bericht: {files}")
        parts.append("\nPrüfe das. Sieh dir die betroffenen Dateien selbst an.")
        return "\n".join(parts)

    async def select_model(self, context: AgentContext):
        """Prefer a different model from the one under review (Spec §32)."""
        selection = await super().select_model(context)
        if self._avoid_model and selection.model.key == self._avoid_model:
            for candidate in selection.fallbacks:
                if candidate.key != self._avoid_model:
                    provider = self._router._catalog.get_provider(candidate.provider)  # noqa: SLF001
                    if provider is not None:
                        selection.model = candidate
                        selection.provider = provider
                        selection.reason += " (anderes Modell für eine unabhängige Prüfung)"
                        break
            else:
                log.info("Kein zweites Modell verfügbar — Prüfung läuft auf demselben Modell")
        return selection

    async def run(self, context: AgentContext) -> AgentResult:
        result = await super().run(context)
        if not result.ok:
            return result
        verdict = parse_review(result.text)
        result.artifacts = verdict
        result.text = render_review(verdict)
        result.ok = verdict["verdict"] != "rejected"
        return result


def parse_review(raw: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", raw or "", re.S)
    payload: dict[str, Any] = {}
    structured = False
    if match:
        try:
            payload = json.loads(match.group(0))
            structured = True
        except json.JSONDecodeError:
            payload = {}

    issues: list[dict[str, str]] = []
    for entry in (payload.get("issues") or [])[:12]:
        if not isinstance(entry, dict) or not entry.get("problem"):
            continue
        severity = str(entry.get("severity", "minor")).lower()
        issues.append({
            "severity": severity if severity in ("blocker", "major", "minor") else "minor",
            "where": str(entry.get("where") or "")[:200],
            "problem": str(entry["problem"])[:500],
            "suggestion": str(entry.get("suggestion") or "")[:500],
        })

    verdict = str(payload.get("verdict") or "").lower()
    if verdict not in ("ok", "needs_changes", "rejected"):
        # An unparseable review must not silently count as approval.
        verdict = "needs_changes" if issues else "ok"
    if any(issue["severity"] == "blocker" for issue in issues) and verdict == "ok":
        verdict = "needs_changes"

    return {
        "verdict": verdict,
        "summary": str(payload.get("summary") or "")[:600] or (raw or "")[:300],
        "issues": issues,
        "blockers": [i for i in issues if i["severity"] == "blocker"],
        # A review whose structure could not be read is reported as such rather than
        # silently counting as approval.
        "structured": structured,
    }


def render_review(verdict: dict[str, Any]) -> str:
    labels = {"ok": "In Ordnung", "needs_changes": "Nachbesserung nötig", "rejected": "Abgelehnt"}
    lines = [f"Prüfung: {labels.get(verdict['verdict'], verdict['verdict'])}"]
    if not verdict.get("structured", True):
        lines.append(
            "(Die Prüfung war nicht strukturiert auswertbar — behandle sie als Hinweis, "
            "nicht als Freigabe.)"
        )
    if verdict["summary"]:
        lines.append(verdict["summary"])
    if verdict["issues"]:
        lines.append("")
        for issue in verdict["issues"]:
            marker = {"blocker": "!!", "major": "!", "minor": "·"}[issue["severity"]]
            where = f" [{issue['where']}]" if issue["where"] else ""
            lines.append(f"{marker}{where} {issue['problem']}")
            if issue["suggestion"]:
                lines.append(f"   Vorschlag: {issue['suggestion']}")
    return "\n".join(lines)
