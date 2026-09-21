"""Tools that let JARVIS use its own memory and scheduler (Spec §24, §27).

These are what make "merke dir das" and "erinnere mich um 18 Uhr" work as actions rather
than as chat: the model calls a tool, the tool runs through the permission engine, and the
result is stored where the user can inspect and delete it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from core.enums import Capability, MemoryKind, RiskLevel
from core.logging_setup import get_logger
from memory import secrets_filter
from memory.manager import MemoryCandidate, memory_manager
from memory.retrieval import retrieval
from tools.base import Tool, ToolContext, ToolResult

log = get_logger("tools.memory")


def _local_timezone(context: ToolContext) -> str:
    return str((context.extra or {}).get("timezone") or "UTC")


class RememberTool(Tool):
    name = "remember"
    description = (
        "Merkt sich eine dauerhaft relevante Information über den Benutzer. "
        "Niemals für Passwörter, Schlüssel oder Tokens verwenden."
    )
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "minLength": 4, "maxLength": 1000,
                        "description": "Eine präzise Aussage, die langfristig gilt"},
            "subject": {"type": "string", "maxLength": 100},
            "kind": {"type": "string",
                     "enum": [k.value for k in MemoryKind], "default": "fact"},
            "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.7},
        },
        "required": ["content"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Dauerhaft merken: {str(arguments.get('content'))[:120]}"

    def scope(self, arguments: dict[str, Any]) -> str:
        return "memory"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        content = arguments["content"].strip()
        subject = (arguments.get("subject") or content[:60]).strip()

        # The filter is checked here as well as in the pipeline: a model must not be able to
        # push a credential into long-term storage by calling the tool directly (Spec §25).
        verdict = secrets_filter.check(content, subject)
        if not verdict.allowed:
            return ToolResult.failure(
                f"Das speichere ich nicht: {verdict.reason}. Zugangsdaten gehören in die "
                "Einstellungen, nicht ins Gedächtnis."
            )

        existing = await retrieval.find_similar(subject, content)
        if existing is not None:
            await memory_manager.update(int(existing["id"]), content=content)
            return ToolResult(
                content=f"Bestehenden Eintrag #{existing['id']} aktualisiert.",
                display={"summary": "Gedächtnis aktualisiert", "id": existing["id"],
                         "subject": subject},
            )

        memory_id = await memory_manager.store(MemoryCandidate(
            content=content,
            subject=subject,
            kind=MemoryKind(arguments.get("kind", "fact")),
            importance=float(arguments.get("importance", 0.7)),
            source="tool",
            conversation_id=context.conversation_id,
        ))
        return ToolResult(
            content=f"Gemerkt (#{memory_id}): {content}",
            display={"summary": f"Gemerkt: {subject}", "id": memory_id},
        )


class RecallTool(Tool):
    name = "recall"
    description = "Durchsucht das Langzeitgedächtnis nach gespeicherten Informationen."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 2},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
        },
        "required": ["query"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Gedächtnis durchsuchen: '{arguments.get('query')}'"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        results = await memory_manager.recall(arguments["query"], arguments.get("limit", 10))
        if not results:
            return ToolResult(
                content="Dazu ist nichts gespeichert.",
                display={"summary": "Keine Treffer", "hits": 0},
            )
        lines = [f"- [{r['kind']}] {r['subject']}: {r['content']}" for r in results]
        return ToolResult(
            content="Gespeicherte Informationen:\n" + "\n".join(lines),
            display={"summary": f"{len(results)} Treffer", "hits": len(results),
                     "subjects": [r["subject"] for r in results[:8]]},
        )


class ForgetTool(Tool):
    name = "forget"
    description = "Löscht einen Gedächtniseintrag. Suche ihn vorher mit recall."
    risk_level = RiskLevel.DESTRUCTIVE
    required_permission = Capability.FILE_DELETE
    input_schema = {
        "type": "object",
        "properties": {"memory_id": {"type": "integer", "minimum": 1}},
        "required": ["memory_id"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Gedächtniseintrag #{arguments.get('memory_id')} LÖSCHEN"

    def scope(self, arguments: dict[str, Any]) -> str:
        return "memory"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        memory_id = int(arguments["memory_id"])
        existing = await retrieval.by_id(memory_id)
        if existing is None:
            return ToolResult.failure(f"Es gibt keinen Eintrag #{memory_id}.")
        await memory_manager.delete(memory_id)
        return ToolResult(
            content=f"Eintrag #{memory_id} gelöscht: {existing['content'][:120]}",
            display={"summary": f"Gelöscht: {existing['subject']}", "id": memory_id},
        )


class CreateReminderTool(Tool):
    name = "create_reminder"
    description = (
        "Legt eine Erinnerung an. Der Zeitpunkt kann als natürliche Sprache angegeben "
        "werden, zum Beispiel 'morgen um 18 Uhr' oder 'jeden Freitag um 17 Uhr'."
    )
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "minLength": 3, "maxLength": 400,
                     "description": "Die vollständige Aussage inklusive Zeitangabe"},
            "timezone": {"type": "string", "default": "Europe/Berlin"},
        },
        "required": ["text"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Erinnerung anlegen: {arguments.get('text')}"

    def scope(self, arguments: dict[str, Any]) -> str:
        return "reminders"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from core.scheduler import scheduler

        timezone = arguments.get("timezone") or _local_timezone(context)
        result = await scheduler.create_from_text(arguments["text"], timezone=timezone)
        if result is None:
            return ToolResult.failure(
                "Ich konnte keinen Zeitpunkt erkennen. Nenne eine Uhrzeit, einen Tag oder "
                "einen Abstand, zum Beispiel 'morgen um 18 Uhr' oder 'in 20 Minuten'."
            )
        reminder_id, parsed = result
        try:
            local = parsed.when.astimezone(ZoneInfo(timezone))
        except Exception:
            local = parsed.when.astimezone(UTC)
        readable = local.strftime("%d.%m.%Y um %H:%M")
        return ToolResult(
            content=f"Erinnerung #{reminder_id} gespeichert für {readable} ({parsed.kind.value}): "
                    f"{parsed.text}",
            display={"summary": f"Erinnerung: {readable}", "id": reminder_id,
                     "when": parsed.when.isoformat(), "kind": parsed.kind.value,
                     "text": parsed.text},
        )


class ListRemindersTool(Tool):
    name = "list_reminders"
    description = "Zeigt die anstehenden Erinnerungen."
    risk_level = RiskLevel.SAFE_READ
    required_permission = Capability.FILE_READ
    input_schema = {"type": "object", "properties": {}}

    def summarise(self, arguments: dict[str, Any]) -> str:
        return "Anstehende Erinnerungen anzeigen"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from core.scheduler import scheduler

        reminders = await scheduler.list_reminders()
        if not reminders:
            return ToolResult(content="Es sind keine Erinnerungen gespeichert.",
                              display={"summary": "Keine Erinnerungen"})
        now = datetime.now(UTC)
        lines = []
        for reminder in reminders[:25]:
            when = datetime.fromisoformat(str(reminder["next_run_at"]).replace("Z", "+00:00"))
            delta = when - now
            hours = delta.total_seconds() / 3600
            relative = (f"in {int(hours)} h" if 1 <= hours < 48
                        else f"in {int(delta.days)} Tagen" if hours >= 48
                        else f"in {max(int(delta.total_seconds() // 60), 0)} min")
            lines.append(f"- #{reminder['id']} {reminder['text']} — {relative} "
                         f"({reminder['schedule_kind']})")
        return ToolResult(
            content="Anstehende Erinnerungen:\n" + "\n".join(lines),
            display={"summary": f"{len(reminders)} Erinnerung(en)", "count": len(reminders)},
        )


class DeleteReminderTool(Tool):
    name = "delete_reminder"
    description = "Löscht eine Erinnerung."
    risk_level = RiskLevel.WRITE
    required_permission = Capability.FILE_WRITE
    input_schema = {
        "type": "object",
        "properties": {"reminder_id": {"type": "integer", "minimum": 1}},
        "required": ["reminder_id"],
    }

    def summarise(self, arguments: dict[str, Any]) -> str:
        return f"Erinnerung #{arguments.get('reminder_id')} löschen"

    def scope(self, arguments: dict[str, Any]) -> str:
        return "reminders"

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from core.scheduler import scheduler

        reminder_id = int(arguments["reminder_id"])
        if not await scheduler.delete_reminder(reminder_id):
            return ToolResult.failure(f"Es gibt keine Erinnerung #{reminder_id}.")
        return ToolResult(content=f"Erinnerung #{reminder_id} gelöscht.",
                          display={"summary": "Erinnerung gelöscht", "id": reminder_id})


MEMORY_TOOLS = [RememberTool(), RecallTool(), ForgetTool()]
SCHEDULE_TOOLS = [CreateReminderTool(), ListRemindersTool(), DeleteReminderTool()]
