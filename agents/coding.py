"""Coding agent (Spec §33, §34).

Workflow: UNDERSTAND → PLAN → INSPECT → EDIT → VALIDATE → REPAIR → REVIEW → FINISH.

The mode decides how far it may go on its own:

``CHAT``             talk about the code, change nothing
``PLAN_ONLY``        produce a plan, change nothing
``ASK_BEFORE_EDIT``  edit, but every write goes through a confirmation (the default)
``AUTO_EDIT``        edit within the trusted folders without asking each time
``FULL_AGENT``       edit, run tests, repair, repeat

The mode never widens permissions: a write still passes the permission engine, so
``AUTO_EDIT`` only means "do not ask me again" where the user already allowed it.
"""

from __future__ import annotations

from typing import Any

from agents.base import Agent, AgentContext, AgentResult
from core.enums import CodingMode
from core.logging_setup import get_logger

log = get_logger("agents.coding")

READ_ONLY_MODES = {CodingMode.CHAT, CodingMode.PLAN_ONLY}

SYSTEM = """Du bist der Coding-Agent von {name}.

Arbeitsweise:
1. VERSTEHEN — lies den relevanten Code, bevor du etwas behauptest.
2. PLANEN — überlege die kleinste Änderung, die das Problem wirklich löst.
3. PRÜFEN — sieh dir die betroffenen Dateien an, rate nie ihren Inhalt.
4. ÄNDERN — ändere so wenig wie möglich; passe dich dem vorhandenen Stil an.
5. VALIDIEREN — führe Tests oder Syntaxprüfungen aus, wenn es sie gibt.
6. REPARIEREN — analysiere Fehler, bevor du es erneut versuchst.
7. BERICHTEN — sage genau, was du geändert hast und was noch offen ist.

Regeln:
- Erfinde niemals Dateiinhalte, Pfade oder Testergebnisse. Lies sie.
- Behaupte nie, etwas geändert oder getestet zu haben, ohne das Werkzeug aufgerufen zu haben.
- Nutze patch_file für gezielte Änderungen; write_file nur für neue Dateien.
- Code, Bezeichner und Kommentare auf Englisch. Erklärungen an mich auf Deutsch.
- Wenn ein Test fehlschlägt, nenne die Ausgabe. Beschönige nichts.
{mode_rules}"""

MODE_RULES = {
    CodingMode.CHAT: (
        "\nModus CHAT: Du darfst NICHTS ändern. Erkläre und schlage vor, mehr nicht."
    ),
    CodingMode.PLAN_ONLY: (
        "\nModus PLAN_ONLY: Du darfst lesen und analysieren, aber nichts schreiben. "
        "Liefere einen konkreten Änderungsplan."
    ),
    CodingMode.ASK_BEFORE_EDIT: (
        "\nModus ASK_BEFORE_EDIT: Jede Änderung wird dem Benutzer zur Bestätigung vorgelegt. "
        "Erkläre vor jeder Änderung kurz, warum sie nötig ist."
    ),
    CodingMode.AUTO_EDIT: (
        "\nModus AUTO_EDIT: Du darfst in den freigegebenen Ordnern ändern, ohne jedes Mal zu "
        "fragen. Prüfe nach jeder Änderung, ob sie funktioniert."
    ),
    CodingMode.FULL_AGENT: (
        "\nModus FULL_AGENT: Arbeite bis zum Ergebnis: ändern, testen, Fehler beheben, erneut "
        "testen. Brich ab und berichte, wenn du zweimal am selben Punkt scheiterst."
    ),
}


class CodingAgent(Agent):
    name = "coding"
    role = "Liest, versteht, ändert und testet Code"
    task_kind = "coding"
    needs_tools = True
    tool_tags = {"files", "terminal", "git"}

    def system_prompt(self, context: AgentContext) -> str:
        mode = context.settings.assistant.coding_mode
        return SYSTEM.format(
            name=context.settings.assistant.name,
            mode_rules=MODE_RULES.get(mode, ""),
        )

    def tools_for(self, context: AgentContext) -> list[Any]:
        specs = super().tools_for(context)
        mode = context.settings.assistant.coding_mode
        if mode in READ_ONLY_MODES:
            # In a read-only mode the writing tools are not offered at all. Relying on the
            # prompt alone would leave a model free to ignore it.
            writing = {"write_file", "patch_file", "delete_file", "delete_directory",
                       "move_file", "copy_file", "create_directory", "git_commit",
                       "git_push", "git_branch", "run_command", "run_python",
                       "run_powershell", "extract_archive", "create_archive"}
            specs = [spec for spec in specs if spec.name not in writing]
        return specs

    def user_prompt(self, context: AgentContext) -> str:
        parts = [context.goal]
        if plan := context.inputs.get("plan"):
            parts.append(f"\nVorgegebener Plan:\n{plan}")
        if review := context.inputs.get("review"):
            parts.append(f"\nAnmerkungen aus der Prüfung, die du berücksichtigen musst:\n{review}")
        return "\n".join(parts)

    async def run(self, context: AgentContext) -> AgentResult:
        result = await super().run(context)
        mode = context.settings.assistant.coding_mode
        result.artifacts["mode"] = mode.value
        result.artifacts["changed_files"] = sorted({
            name for name in result.tool_calls
            if name in {"write_file", "patch_file", "move_file", "delete_file"}
        })
        result.artifacts["ran_validation"] = any(
            name in {"run_command", "run_python", "run_powershell"} for name in result.tool_calls
        )
        return result
