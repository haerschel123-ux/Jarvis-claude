"""Assistant core (Spec §13).

The single place where a user message becomes an answer. The pipeline is explicit rather than
a bare ``send_to_llm()`` call:

    intent classification
      → permission context
      → context building (memory, project, files, history)
      → model routing
      → generation (streaming, with tool calls)
      → result validation
      → memory integration
      → response

Stage hooks (tools, memory, agents) are injected rather than imported, so this module stays
free of cycles and each subsystem can be developed and tested on its own.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.config import Settings, get_settings
from core.context import ContextBuilder, Priority
from core.enums import AssistantState, Intent
from core.errors import JarvisError, ProviderError
from core.events import EventType, event_bus
from core.intent import IntentResult, classify_rules
from core.logging_setup import get_logger
from core.model_profiles import Requirements
from core.model_router import ModelRouter, ModelSelection, router
from core.platform_info import capabilities as platform_capabilities
from core.prompts import build_system_prompt
from memory.conversations import ConversationStore, conversations
from providers.base import Message, StreamChunk, ToolCall, ToolSpec, Usage

log = get_logger("assistant")

# Intents that need a model able to call tools, and those that need to see images.
TOOL_INTENTS = frozenset({
    Intent.FILE_TASK, Intent.CODING_TASK, Intent.SYSTEM_ACTION, Intent.SCREEN_ACTION,
    Intent.WEB_RESEARCH, Intent.REMINDER, Intent.CALENDAR_ACTION, Intent.EMAIL_ACTION,
    Intent.DISCORD_ACTION, Intent.GITHUB_ACTION, Intent.DAYZ_ACTION,
    Intent.SMART_HOME_ACTION, Intent.MULTI_STEP_TASK, Intent.MEMORY_ACTION,
})

# Which routing profile each intent maps to.
INTENT_TASK_KIND: dict[Intent, str] = {
    Intent.CONVERSATION: "chat",
    Intent.QUESTION: "chat",
    Intent.WEB_RESEARCH: "research",
    Intent.FILE_TASK: "chat",
    Intent.CODING_TASK: "coding",
    Intent.SYSTEM_ACTION: "chat",
    Intent.SCREEN_ACTION: "vision",
    Intent.MEMORY_ACTION: "chat",
    Intent.REMINDER: "chat",
    Intent.CALENDAR_ACTION: "chat",
    Intent.EMAIL_ACTION: "chat",
    Intent.DISCORD_ACTION: "coding",
    Intent.GITHUB_ACTION: "coding",
    Intent.DAYZ_ACTION: "coding",
    Intent.SMART_HOME_ACTION: "chat",
    Intent.MULTI_STEP_TASK: "planning",
}

MAX_TOOL_ROUNDS = 8


class ToolExecutor(Protocol):
    """Implemented by the tool engine in stage 2."""

    def specs_for(self, intent: Intent, settings: Settings) -> list[ToolSpec]: ...

    async def execute(
        self, call: ToolCall, *, context: TurnContext
    ) -> ToolOutcome: ...


@dataclass(slots=True)
class ToolOutcome:
    call_id: str
    tool: str
    ok: bool
    content: str
    display: dict[str, Any] = field(default_factory=dict)


MemoryRecall = Callable[[str, int], Awaitable[list[dict[str, Any]]]]
MemoryCapture = Callable[["TurnContext"], Awaitable[None]]


@dataclass
class TurnContext:
    """Everything one turn needs and produces."""

    conversation_id: int
    user_message: str
    settings: Settings
    intent: IntentResult
    images: list[str] = field(default_factory=list)
    project_id: int | None = None
    project_instructions: str = ""
    explicit_model: str | None = None
    selection: ModelSelection | None = None
    agent_decision: Any = None
    history: list[Message] = field(default_factory=list)
    working_messages: list[Message] = field(default_factory=list)
    tool_outcomes: list[ToolOutcome] = field(default_factory=list)
    citations: list[dict[str, Any]] = field(default_factory=list)
    answer: str = ""
    usage: Usage = field(default_factory=Usage)
    started_at: float = field(default_factory=time.time)
    cancelled: bool = False

    @property
    def duration_ms(self) -> int:
        return int((time.time() - self.started_at) * 1000)


@dataclass(slots=True)
class TurnEvent:
    """What the API layer forwards to the client."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)


class AssistantCore:
    def __init__(
        self,
        *,
        model_router: ModelRouter | None = None,
        conversation_store: ConversationStore | None = None,
    ) -> None:
        self._router = model_router or router
        self._conversations = conversation_store or conversations
        self._tools: ToolExecutor | None = None
        self._orchestrator: Any | None = None
        self._recall: MemoryRecall | None = None
        self._capture: MemoryCapture | None = None
        self._state = AssistantState.IDLE

    # --- wiring ---------------------------------------------------------------------

    def set_tool_executor(self, executor: ToolExecutor | None) -> None:
        self._tools = executor

    def set_memory_hooks(self, recall: MemoryRecall | None, capture: MemoryCapture | None) -> None:
        self._recall = recall
        self._capture = capture

    def set_orchestrator(self, orchestrator: Any | None) -> None:
        """Wire in the agent system. Without it, every request takes the direct path."""
        self._orchestrator = orchestrator

    @property
    def state(self) -> AssistantState:
        return self._state

    def _set_state(self, state: AssistantState, **data: Any) -> None:
        self._state = state
        event_bus.emit(
            {
                AssistantState.IDLE: EventType.ASSISTANT_IDLE,
                AssistantState.THINKING: EventType.ASSISTANT_THINKING,
                AssistantState.ACTING: EventType.ASSISTANT_ACTING,
                AssistantState.SPEAKING: EventType.ASSISTANT_SPEAKING,
                AssistantState.ERROR: EventType.ASSISTANT_ERROR,
            }.get(state, EventType.ASSISTANT_IDLE),
            state=state.value,
            **data,
        )

    # --- main entry point -----------------------------------------------------------

    async def run_turn(
        self,
        user_message: str,
        *,
        conversation_id: int | None = None,
        images: Sequence[str] | None = None,
        project_id: int | None = None,
        explicit_model: str | None = None,
        settings: Settings | None = None,
        source: str = "text",
    ) -> AsyncIterator[TurnEvent]:
        """Run one full turn, yielding events as they happen."""
        settings = settings or get_settings()
        images = list(images or [])

        conversation_id = await self._conversations.ensure(
            conversation_id, source=source, project_id=project_id
        )
        existing = await self._conversations.messages(conversation_id, limit=1)
        intent = classify_rules(user_message)

        context = TurnContext(
            conversation_id=conversation_id,
            user_message=user_message,
            settings=settings,
            intent=intent,
            images=images,
            project_id=project_id,
            explicit_model=explicit_model,
        )

        yield TurnEvent("turn.started", {
            "conversation_id": conversation_id,
            "intent": intent.to_dict(),
        })
        event_bus.emit(EventType.CHAT_STARTED, conversation_id=conversation_id,
                       intent=intent.intent.value)

        await self._conversations.add_message(
            conversation_id, "user", user_message, trust="user",
            attachments=[{"kind": "image"} for _ in images] or None,
        )
        if not existing:
            title = await self._conversations.auto_title(conversation_id, user_message)
            yield TurnEvent("conversation.titled", {"title": title})

        try:
            async for event in self._generate(context):
                yield event
        except JarvisError as exc:
            self._set_state(AssistantState.ERROR, error=type(exc).__name__)
            await self._conversations.add_message(
                conversation_id, "assistant", exc.user_message, error=str(exc)
            )
            event_bus.emit(EventType.CHAT_FAILED, conversation_id=conversation_id,
                           error=type(exc).__name__)
            yield TurnEvent("turn.failed", exc.to_dict())
            return
        except Exception as exc:  # unexpected: log fully, tell the user plainly
            log.exception("Unbehandelter Fehler in run_turn")
            self._set_state(AssistantState.ERROR, error=type(exc).__name__)
            message = f"Unerwarteter Fehler: {type(exc).__name__}"
            await self._conversations.add_message(
                conversation_id, "assistant", message, error=str(exc)
            )
            event_bus.emit(EventType.CHAT_FAILED, conversation_id=conversation_id,
                           error=type(exc).__name__)
            yield TurnEvent("turn.failed", {"error": type(exc).__name__, "user_message": message})
            return

        self._set_state(AssistantState.IDLE)
        event_bus.emit(EventType.CHAT_COMPLETED, conversation_id=conversation_id,
                       duration_ms=context.duration_ms)
        yield TurnEvent("turn.completed", {
            "conversation_id": conversation_id,
            "duration_ms": context.duration_ms,
            "usage": context.usage.to_dict(),
            "model": context.selection.model.key if context.selection else None,
            "tools_used": [o.tool for o in context.tool_outcomes],
        })

    # --- pipeline -------------------------------------------------------------------

    async def _generate(self, context: TurnContext) -> AsyncIterator[TurnEvent]:
        settings = context.settings
        intent = context.intent.intent

        self._set_state(AssistantState.THINKING, stage="routing")
        yield TurnEvent("status", {"stage": "Analysiere", "intent": intent.value})

        tool_specs: list[ToolSpec] = []
        if self._tools is not None and intent in TOOL_INTENTS:
            tool_specs = self._tools.specs_for(intent, settings)

        requirements = Requirements(
            needs_tools=bool(tool_specs),
            needs_vision=bool(context.images),
        )
        if context.selection is None:
            context.selection = await self._router.select(
                INTENT_TASK_KIND.get(intent, "chat"),
                requirements=requirements,
                explicit_model=context.explicit_model,
                settings=settings,
            )
        selection = context.selection

        yield TurnEvent("model.selected", selection.to_dict())

        # A model without confirmed tool support must not be offered tools: it would either
        # ignore them or fail the request.
        if tool_specs and selection.model.supports_tools is not True and not selection.model.is_router:
            log.info("Modell %s bietet kein bestätigtes Tool-Calling — Tools deaktiviert",
                     selection.model.key)
            tool_specs = []

        context.history = await self._conversations.history(context.conversation_id)
        # The user's current message is added by the builder, so drop the copy just persisted.
        if context.history and context.history[-1].role == "user":
            context.history = context.history[:-1]

        # How much machinery does this deserve? (Spec §30 — do not start five agents for a
        # small question.) The decision is rule-based, so it costs nothing.
        if self._orchestrator is not None:
            decision = self._orchestrator.decide(
                context.user_message, context.intent, settings, has_tools=bool(tool_specs)
            )
            context.agent_decision = decision
            yield TurnEvent("agent.decision", decision.to_dict())

        memories: list[dict[str, Any]] = []
        if self._recall is not None and settings.memory.mode.value != "OFF":
            yield TurnEvent("status", {"stage": "Gedächtnis"})
            memories = await self._recall(
                context.user_message, settings.memory.max_memories_in_context
            )

        builder = ContextBuilder(
            selection.model.context_length,
            output_reserve=settings.models.max_output_tokens,
        )
        builder.add_memories(memories)
        if context.project_instructions:
            builder.add(Priority.PROJECT_INSTRUCTIONS, "Projekt", context.project_instructions)

        caps = platform_capabilities()
        unavailable = [
            f"{name}: {info['reason']}" for name, info in caps.items() if not info["available"]
        ]
        system_prompt = build_system_prompt(
            settings,
            capabilities=[t.name for t in tool_specs] or None,
            unavailable=unavailable or None,
            project_instructions=context.project_instructions,
            include_tools=bool(tool_specs),
        )

        built = builder.build(
            system_prompt=system_prompt,
            user_message=context.user_message,
            history=context.history,
            user_images=context.images,
        )
        context.working_messages = list(built.messages)
        yield TurnEvent("context.built", built.to_dict())

        # An escalated request runs through the agent system; everything else keeps the
        # direct, single-model path, which is faster and cheaper.
        if self._orchestrator is not None and context.agent_decision is not None:
            from agents.coordinator import Strategy

            if context.agent_decision.strategy.value >= Strategy.SPECIALIST.value:
                async for event in self._run_agents(context, memories):
                    yield event
                return

        async for event in self._run_model_loop(context, tool_specs):
            yield event

        if self._capture is not None and settings.memory.mode.value in ("AUTO", "IMPORTANT_ONLY"):
            try:
                await self._capture(context)
            except Exception:
                log.exception("Gedächtnis-Erfassung fehlgeschlagen")

    async def _run_agents(
        self, context: TurnContext, memories: list[dict[str, Any]]
    ) -> AsyncIterator[TurnEvent]:
        """Hand the request to the agent system and stream its progress."""
        from agents.base import AgentContext

        decision = context.agent_decision
        self._set_state(AssistantState.ACTING, agents=decision.agents)
        yield TurnEvent("status", {"stage": _agent_stage(decision)})

        agent_context = AgentContext(
            goal=context.user_message,
            settings=context.settings,
            conversation_id=context.conversation_id,
            project_id=context.project_id,
            project_instructions=context.project_instructions,
            history=context.history,
            memories=memories,
        )
        run = await self._orchestrator.run(decision, agent_context)

        context.answer = run.text or "Der Agentenlauf hat kein Ergebnis geliefert."
        context.usage = _sum_usage(step.usage for step in run.steps)
        for step in run.steps:
            yield TurnEvent("agent.step", step.to_dict())
            context.tool_outcomes.extend(
                ToolOutcome(call_id="", tool=name, ok=True, content="")
                for name in step.tool_calls
            )

        yield TurnEvent("delta", {"text": context.answer})
        await self._conversations.add_message(
            context.conversation_id, "assistant", context.answer,
            trust="system",
            model_id=run.steps[-1].model if run.steps else None,
            agent=decision.agents[0] if decision.agents else "coordinator",
            prompt_tokens=context.usage.prompt_tokens,
            completion_tokens=context.usage.completion_tokens,
            cost_usd=context.usage.cost_usd,
            error=None if run.ok else "agent run failed",
        )

    async def _run_model_loop(
        self, context: TurnContext, tool_specs: list[ToolSpec]
    ) -> AsyncIterator[TurnEvent]:
        """Generate, run any requested tools, and generate again until the model is done."""
        selection = context.selection
        assert selection is not None

        for round_index in range(MAX_TOOL_ROUNDS):
            self._set_state(AssistantState.THINKING, stage="generating", round=round_index)
            chunks_text: list[str] = []
            tool_calls: list[ToolCall] = []

            async for chunk in self._stream_with_fallback(context, tool_specs):
                if chunk.type == "text":
                    chunks_text.append(chunk.text)
                    event_bus.emit(EventType.CHAT_DELTA,
                                   conversation_id=context.conversation_id, text=chunk.text)
                    yield TurnEvent("delta", {"text": chunk.text})
                elif chunk.type == "reasoning":
                    yield TurnEvent("reasoning", {"text": chunk.text})
                elif chunk.type == "tool_call" and chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)
                elif chunk.type == "usage" and chunk.usage is not None:
                    context.usage = _add_usage(context.usage, chunk.usage)

            text = "".join(chunks_text)
            context.answer += text

            if not tool_calls:
                await self._conversations.add_message(
                    context.conversation_id, "assistant", context.answer,
                    trust="system",
                    model_id=selection.model.key,
                    citations=context.citations or None,
                    prompt_tokens=context.usage.prompt_tokens,
                    completion_tokens=context.usage.completion_tokens,
                    cost_usd=context.usage.cost_usd,
                )
                return

            if self._tools is None:
                # The model asked for a tool but none are wired up: say so rather than
                # pretending the action happened (Spec §80).
                note = "Ich wollte ein Werkzeug verwenden, aber es ist keines verfügbar."
                context.answer += ("\n\n" if context.answer else "") + note
                await self._conversations.add_message(
                    context.conversation_id, "assistant", context.answer,
                    trust="system", model_id=selection.model.key,
                )
                yield TurnEvent("delta", {"text": note})
                return

            context.working_messages.append(
                Message("assistant", text, tool_calls=tool_calls, trust="system")
            )
            self._set_state(AssistantState.ACTING, tools=[c.name for c in tool_calls])

            for call in tool_calls:
                yield TurnEvent("tool.requested", {"tool": call.name, "arguments": call.arguments})
                outcome = await self._tools.execute(call, context=context)
                context.tool_outcomes.append(outcome)
                yield TurnEvent("tool.result", {
                    "tool": outcome.tool, "ok": outcome.ok, "display": outcome.display,
                })
                context.working_messages.append(
                    Message("tool", outcome.content, name=outcome.tool,
                            tool_call_id=outcome.call_id, trust="tool_result")
                )
            if context.cancelled:
                yield TurnEvent("turn.cancelled", {})
                return
        else:
            # Loop limit reached: stop instead of spinning forever (Spec §101).
            note = (
                f"Ich habe nach {MAX_TOOL_ROUNDS} Werkzeugrunden abgebrochen, um keine "
                "Endlosschleife zu erzeugen. Sag mir, wie ich weitermachen soll."
            )
            context.answer += ("\n\n" if context.answer else "") + note
            await self._conversations.add_message(
                context.conversation_id, "assistant", context.answer,
                trust="system", model_id=selection.model.key,
            )
            yield TurnEvent("delta", {"text": note})

    async def _stream_with_fallback(
        self, context: TurnContext, tool_specs: list[ToolSpec]
    ) -> AsyncIterator[StreamChunk]:
        """Stream from the chosen model, falling through the fallback chain on failure."""
        selection = context.selection
        assert selection is not None
        settings = context.settings

        attempts = [(selection.model, selection.provider)]
        from providers.catalog import catalog as global_catalog

        for fallback in selection.fallbacks:
            provider = global_catalog.get_provider(fallback.provider)
            if provider is not None:
                attempts.append((fallback, provider))

        last_error: Exception | None = None
        for index, (model, provider) in enumerate(attempts):
            try:
                produced = False
                async for chunk in provider.stream_chat(
                    context.working_messages,
                    model.id,
                    tools=tool_specs or None,
                    temperature=settings.models.temperature,
                    max_tokens=settings.models.max_output_tokens,
                ):
                    produced = True
                    yield chunk
                if produced:
                    if index > 0:
                        log.info("Antwort über Rückfallmodell %s erzeugt", model.key)
                        context.selection.model = model
                    return
                last_error = ProviderError(f"{model.key} lieferte keine Antwort")
            except JarvisError as exc:
                last_error = exc
                log.warning("Modell %s fehlgeschlagen (%s) — versuche das nächste",
                            model.key, type(exc).__name__)
            except Exception as exc:
                last_error = exc
                log.exception("Unerwarteter Fehler bei Modell %s", model.key)

        raise last_error or ProviderError("Kein Modell konnte antworten")


def _agent_stage(decision: Any) -> str:
    names = " → ".join(decision.agents) if decision.agents else "Agent"
    return f"Agenten arbeiten: {names}"


def _sum_usage(items) -> Usage:  # noqa: ANN001
    total = Usage()
    for usage in items:
        total = _add_usage(total, usage)
    return total


def _add_usage(left: Usage, right: Usage) -> Usage:
    def add(a: int | None, b: int | None) -> int | None:
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    def addf(a: float | None, b: float | None) -> float | None:
        if a is None and b is None:
            return None
        return (a or 0.0) + (b or 0.0)

    return Usage(
        prompt_tokens=add(left.prompt_tokens, right.prompt_tokens),
        completion_tokens=add(left.completion_tokens, right.completion_tokens),
        total_tokens=add(left.total_tokens, right.total_tokens),
        cost_usd=addf(left.cost_usd, right.cost_usd),
    )


assistant = AssistantCore()
