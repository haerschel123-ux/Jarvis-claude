"""Planner agent (Spec §31).

Turns a goal into a small, concrete, ordered plan. It plans only — it never edits anything,
so a bad plan costs a model call rather than a broken file.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agents.base import Agent, AgentContext, AgentResult
from core.logging_setup import get_logger

log = get_logger("agents.planner")

MAX_STEPS = 8

SYSTEM = """Du bist der Planer von {name}. Du zerlegst ein Ziel in wenige, konkrete Schritte.

Regeln:
- Höchstens {max_steps} Schritte. Weniger ist besser.
- Jeder Schritt ist eine einzelne, überprüfbare Handlung.
- Nenne für jeden Schritt, woran man erkennt, dass er geglückt ist.
- Plane nur das, was das Ziel wirklich verlangt. Erfinde keine Zusatzarbeit.
- Du führst nichts aus. Du änderst keine Dateien. Du planst ausschließlich.
- Wenn dir eine Information fehlt, plane als ersten Schritt, sie zu beschaffen.
- Wenn das Ziel in einem Schritt erledigt ist, gib genau einen Schritt zurück.

Antworte ausschließlich mit JSON:
{{"plan": [{{"title": "kurz", "detail": "was genau zu tun ist",
            "done_when": "woran man den Erfolg erkennt"}}],
  "risks": ["was schiefgehen kann"],
  "needs_confirmation": true/false}}"""


class PlannerAgent(Agent):
    name = "planner"
    role = "Zerlegt ein Ziel in überprüfbare Schritte"
    task_kind = "planning"
    needs_reasoning = False
    max_output_tokens = 1200

    def system_prompt(self, context: AgentContext) -> str:
        return SYSTEM.format(
            name=context.settings.assistant.name, max_steps=MAX_STEPS
        )

    def user_prompt(self, context: AgentContext) -> str:
        return f"Ziel:\n{context.goal}"

    async def run(self, context: AgentContext) -> AgentResult:
        result = await super().run(context)
        if not result.ok:
            return result
        plan = parse_plan(result.text)
        if not plan["plan"]:
            # A planner that produced nothing usable must say so rather than let the caller
            # proceed with an empty plan.
            result.ok = False
            result.error = "Der Planer hat keinen verwertbaren Plan geliefert."
            result.text = result.error
            return result
        result.artifacts = plan
        result.text = render_plan(plan)
        return result


def parse_plan(raw: str) -> dict[str, Any]:
    """Parse the planner's JSON, tolerating surrounding prose and mild malformation."""
    match = re.search(r"\{.*\}", raw or "", re.S)
    payload: dict[str, Any] = {}
    if match:
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            payload = {}

    steps: list[dict[str, str]] = []
    for entry in (payload.get("plan") or [])[:MAX_STEPS]:
        if isinstance(entry, str):
            steps.append({"title": entry[:120], "detail": "", "done_when": ""})
        elif isinstance(entry, dict) and entry.get("title"):
            steps.append({
                "title": str(entry["title"])[:120],
                "detail": str(entry.get("detail") or "")[:600],
                "done_when": str(entry.get("done_when") or "")[:300],
            })

    if not steps and raw:
        # Fall back to numbered lines, which models produce when they ignore the format.
        for line in raw.splitlines():
            if bullet := re.match(r"^\s*(?:\d+[.)]|[-*])\s+(.{4,120})", line):
                steps.append({"title": bullet.group(1).strip(), "detail": "", "done_when": ""})
            if len(steps) >= MAX_STEPS:
                break

    risks = [str(r)[:200] for r in (payload.get("risks") or []) if r][:5]
    return {
        "plan": steps,
        "risks": risks,
        "needs_confirmation": bool(payload.get("needs_confirmation")),
    }


def render_plan(plan: dict[str, Any]) -> str:
    lines = ["Plan:"]
    for index, step in enumerate(plan["plan"], 1):
        lines.append(f"{index}. {step['title']}")
        if step.get("detail"):
            lines.append(f"   {step['detail']}")
        if step.get("done_when"):
            lines.append(f"   Fertig, wenn: {step['done_when']}")
    if plan.get("risks"):
        lines.append("\nRisiken:")
        lines.extend(f"- {risk}" for risk in plan["risks"])
    return "\n".join(lines)
