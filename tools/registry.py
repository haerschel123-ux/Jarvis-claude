"""Tool registry and execution (Spec §67, §68, §69, §89).

Every tool call goes through :meth:`ToolRegistry.execute`, which always, in this order:

1. resolves the tool and validates the arguments server-side
2. checks the emergency stop
3. asks the permission engine (and the user, when required)
4. runs the tool with a timeout and a cancellation scope
5. records the run in ``tool_runs`` and in ``audit.log``, and emits UI events

There is no path around this. A tool implementation never performs its own permission check,
so it cannot forget one.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Iterable
from typing import Any

from core.config import Settings, get_settings
from core.emergency import emergency
from core.enums import Capability, Intent, RiskLevel
from core.errors import Cancelled, JarvisError, PermissionDenied, ToolError, ToolNotFound
from core.events import EventType, event_bus
from core.logging_setup import audit, get_logger
from core.permissions import PermissionRequest, permissions
from core.redaction import redact_value
from memory.database import Database, db
from providers.base import ToolSpec
from tools.base import Tool, ToolContext, ToolResult, validate_arguments

log = get_logger("tools")
tool_log = get_logger("jarvis.tools")

DEFAULT_TIMEOUT = 120.0

# Which tools each intent is offered. Handing a model twenty irrelevant tools makes it worse
# at choosing, so the set is narrowed to what the request plausibly needs (Spec §30).
INTENT_TOOL_TAGS: dict[Intent, set[str]] = {
    Intent.FILE_TASK: {"files"},
    Intent.CODING_TASK: {"files", "terminal", "git"},
    Intent.SYSTEM_ACTION: {"apps", "system", "terminal"},
    Intent.SCREEN_ACTION: {"screen", "input", "apps"},
    Intent.WEB_RESEARCH: {"web"},
    Intent.MEMORY_ACTION: {"memory"},
    Intent.REMINDER: {"schedule"},
    Intent.CALENDAR_ACTION: {"calendar", "schedule"},
    Intent.EMAIL_ACTION: {"email"},
    Intent.DISCORD_ACTION: {"discord", "files", "terminal"},
    Intent.GITHUB_ACTION: {"git", "github", "files"},
    Intent.DAYZ_ACTION: {"dayz", "files", "nitrado"},
    Intent.SMART_HOME_ACTION: {"smart_home"},
    Intent.MULTI_STEP_TASK: {"files", "terminal", "web", "apps", "git", "schedule"},
    Intent.QUESTION: {"web"},
}


class ToolRegistry:
    def __init__(self, database: Database | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._tags: dict[str, set[str]] = {}
        self._db = database or db

    # --- registration ---------------------------------------------------------------

    def register(self, tool: Tool, tags: Iterable[str] = ()) -> None:
        if not tool.name:
            raise ValueError("A tool needs a name")
        if tool.name in self._tools:
            log.debug("Werkzeug %s wird ersetzt", tool.name)
        self._tools[tool.name] = tool
        self._tags[tool.name] = set(tags)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._tags.pop(name, None)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self, *, include_unavailable: bool = False) -> list[Tool]:
        tools = sorted(self._tools.values(), key=lambda t: t.name)
        if include_unavailable:
            return tools
        return [t for t in tools if t.available]

    def clear(self) -> None:
        self._tools.clear()
        self._tags.clear()

    # --- selection ------------------------------------------------------------------

    def specs_for(self, intent: Intent, settings: Settings | None = None) -> list[ToolSpec]:
        """Tools offered to the model for this intent, minus anything set to DENY."""
        settings = settings or get_settings()
        tags = INTENT_TOOL_TAGS.get(intent, set())
        specs: list[ToolSpec] = []
        for tool in self.all():
            if tags and not (self._tags.get(tool.name, set()) & tags):
                continue
            # A capability the user set to DENY is not even advertised: offering a tool that
            # can only ever be refused wastes a round trip and confuses the model.
            if settings.permission_for(tool.required_permission).value == "DENY":
                continue
            specs.append(tool.to_spec())
        return specs

    def describe_all(self, settings: Settings | None = None) -> list[dict[str, Any]]:
        settings = settings or get_settings()
        result = []
        for tool in self.all(include_unavailable=True):
            entry = tool.to_dict()
            entry["tags"] = sorted(self._tags.get(tool.name, set()))
            entry["permission"] = settings.permission_for(tool.required_permission).value
            result.append(entry)
        return result

    # --- execution ------------------------------------------------------------------

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        context: ToolContext | None = None,
        settings: Settings | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        call_id: str = "",
    ) -> ToolResult:
        """Run one tool with the full permission, audit and cancellation machinery."""
        settings = settings or get_settings()
        context = context or ToolContext()
        context.settings = settings
        started = time.perf_counter()

        tool = self._tools.get(name)
        if tool is None:
            available = ", ".join(self.names()) or "keine"
            raise ToolNotFound(
                f"Unknown tool: {name}",
                user_message=f"Das Werkzeug '{name}' gibt es nicht. Verfügbar: {available}",
            )
        if not tool.available:
            return ToolResult.failure(
                tool.unavailable_reason
                or f"'{name}' ist auf diesem System nicht verfügbar.",
                unavailable=True,
            )

        event_bus.emit(EventType.TOOL_REQUESTED, tool=name, arguments=redact_value(arguments))

        # 1. Server-side argument validation — model output is never trusted (Spec §67).
        cleaned = validate_arguments(tool.input_schema, arguments, name)

        # 2. The emergency stop beats everything. It is a deliberate user action, so it is
        #    reported as a refused call rather than raised at the caller.
        if emergency.engaged:
            tool_log.info("STOPPED %s: Not-Stopp ist aktiv", name)
            return ToolResult.failure(
                "Not-Stopp ist aktiv — ich führe keine Aktionen aus.", stopped=True
            )

        # 3. Permission. A tool may refine its risk for these specific arguments.
        try:
            risk, capability = tool.risk_for(cleaned)
        except Exception:
            log.exception("risk_for() von %s ist fehlgeschlagen — nutze den deklarierten Wert", name)
            risk, capability = tool.risk_level, tool.required_permission

        request = PermissionRequest(
            tool=name,
            capability=capability,
            risk=risk,
            arguments=cleaned,
            summary=tool.summarise(cleaned),
            scope=tool.scope(cleaned),
            agent=context.agent,
        )
        run_id = await self._record_start(tool, cleaned, context, settings, risk, capability)

        try:
            decision = await permissions.authorise(request, settings)
        except PermissionDenied as exc:
            await self._record_end(run_id, "denied", started, error=exc.user_message,
                                   decision="denied")
            tool_log.info("DENIED %s: %s", name, exc.user_message)
            return ToolResult.failure(exc.user_message, denied=True)

        # 4. Run.
        event_bus.emit(EventType.TOOL_STARTED, tool=name, risk=risk.value)
        tool_log.info("START %s %s", name, json.dumps(redact_value(cleaned), ensure_ascii=False,
                                                      default=str)[:500])
        cancel = context.cancel or asyncio.Event()
        context.cancel = cancel
        emergency.register_scope(cancel)

        try:
            result = await asyncio.wait_for(tool.run(cleaned, context), timeout=timeout)
        except TimeoutError:
            message = f"'{name}' hat das Zeitlimit von {timeout:.0f}s überschritten."
            await self._record_end(run_id, "failed", started, error=message)
            event_bus.emit(EventType.TOOL_FAILED, tool=name, error="timeout")
            tool_log.warning("TIMEOUT %s", name)
            return ToolResult.failure(message, timeout=True)
        except (Cancelled, asyncio.CancelledError):
            await self._record_end(run_id, "failed", started, error="abgebrochen")
            event_bus.emit(EventType.TOOL_FAILED, tool=name, error="cancelled")
            return ToolResult.failure("Der Vorgang wurde abgebrochen.", cancelled=True)
        except JarvisError as exc:
            await self._record_end(run_id, "failed", started, error=str(exc))
            event_bus.emit(EventType.TOOL_FAILED, tool=name, error=type(exc).__name__)
            tool_log.warning("FAILED %s: %s", name, exc)
            return ToolResult.failure(exc.user_message)
        except Exception as exc:
            log.exception("Werkzeug %s ist unerwartet fehlgeschlagen", name)
            await self._record_end(run_id, "failed", started, error=f"{type(exc).__name__}: {exc}")
            event_bus.emit(EventType.TOOL_FAILED, tool=name, error=type(exc).__name__)
            return ToolResult.failure(f"{name} ist fehlgeschlagen: {type(exc).__name__}")
        finally:
            emergency.unregister_scope(cancel)

        # 5. Record.
        status = "success" if result.ok else "failed"
        await self._record_end(
            run_id, status, started,
            summary=result.display.get("summary", "") or result.content[:200],
            error=result.error or None,
            decision=decision.decision.value,
        )
        event_bus.emit(
            EventType.TOOL_COMPLETED if result.ok else EventType.TOOL_FAILED,
            tool=name, ok=result.ok, display=redact_value(result.display),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        tool_log.info("%s %s (%dms)", status.upper(), name,
                      int((time.perf_counter() - started) * 1000))
        return result

    # --- persistence ----------------------------------------------------------------

    async def _record_start(
        self,
        tool: Tool,
        arguments: dict[str, Any],
        context: ToolContext,
        settings: Settings,
        risk: RiskLevel,
        capability: Capability,
    ) -> int | None:
        try:
            return await self._db.execute(
                """
                INSERT INTO tool_runs
                    (tool, agent, task_id, conversation_id, arguments, risk_level,
                     permission, decision, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'started')
                """,
                (
                    tool.name,
                    context.agent or None,
                    context.task_id,
                    context.conversation_id,
                    json.dumps(redact_value(arguments), ensure_ascii=False, default=str),
                    risk.value,
                    settings.permission_for(capability).value,
                    "pending",
                ),
            )
        except Exception:
            # Audit persistence must never break the action itself; the log file still has it.
            log.exception("tool_runs-Eintrag konnte nicht angelegt werden")
            return None

    async def _record_end(
        self,
        run_id: int | None,
        status: str,
        started: float,
        *,
        summary: str = "",
        error: str | None = None,
        decision: str = "allowed",
    ) -> None:
        duration_ms = int((time.perf_counter() - started) * 1000)
        if run_id is not None:
            try:
                await self._db.execute(
                    """
                    UPDATE tool_runs
                       SET status = ?, result_summary = ?, error = ?, duration_ms = ?,
                           decision = ?
                     WHERE id = ?
                    """,
                    (status, summary[:500], error, duration_ms, decision, run_id),
                )
            except Exception:
                log.exception("tool_runs-Eintrag konnte nicht aktualisiert werden")
        audit(
            f"tool.{status}",
            tool_run_id=run_id,
            status=status,
            decision=decision,
            duration_ms=duration_ms,
            error=error,
        )

    async def recent_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._db.fetch_all(
            "SELECT * FROM tool_runs ORDER BY id DESC LIMIT ?", (limit,)
        )


registry = ToolRegistry()


class RegistryToolExecutor:
    """Adapter that lets the assistant core drive the registry (Spec §13)."""

    def __init__(self, tool_registry: ToolRegistry | None = None) -> None:
        self._registry = tool_registry or registry

    def specs_for(self, intent: Intent, settings: Settings) -> list[ToolSpec]:
        return self._registry.specs_for(intent, settings)

    async def execute(self, call, *, context):  # noqa: ANN001 - matches the core's protocol
        from core.assistant import ToolOutcome

        tool_context = ToolContext(
            conversation_id=context.conversation_id,
            agent=context.intent.intent.value,
        )
        try:
            result = await self._registry.execute(
                call.name, call.arguments, context=tool_context,
                settings=context.settings, call_id=call.id,
            )
        except ToolNotFound as exc:
            return ToolOutcome(call.id, call.name, False, exc.user_message,
                               {"error": exc.user_message})
        except ToolError as exc:
            return ToolOutcome(call.id, call.name, False, exc.user_message,
                               {"error": exc.user_message})
        if result.citations:
            context.citations.extend(result.citations)
        return ToolOutcome(
            call.id, call.name, result.ok, result.content, result.display
        )


def risk_of(tool_name: str) -> RiskLevel | None:
    tool = registry.get(tool_name)
    return tool.risk_level if tool else None


def capability_of(tool_name: str) -> Capability | None:
    tool = registry.get(tool_name)
    return tool.required_permission if tool else None
