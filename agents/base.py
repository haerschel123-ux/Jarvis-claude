"""Agent foundation (Spec §29, §31).

An agent is a role with its own system prompt, its own model requirements and its own tool
subset. Every agent goes through the same machinery:

* the **model router** picks its model, honouring FREE_ONLY and the user's priorities
* the **tool registry** runs its tools, so permissions and the audit trail apply unchanged
* the **event bus** reports its progress, so the UI can show what is happening

Agents never call a provider directly and never bypass the permission engine.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from core.config import Settings, get_settings
from core.context import ContextBuilder, Priority
from core.errors import Cancelled, JarvisError
from core.events import EventType, event_bus
from core.logging_setup import get_logger
from core.model_profiles import Requirements
from core.model_router import ModelRouter, ModelSelection, router
from core.prompts import wrap_tool_result
from providers.base import ChatResult, Message, ToolCall, ToolSpec, Usage

log = get_logger("agents")

MAX_TOOL_ROUNDS = 6


@dataclass
class AgentContext:
    """What an agent is asked to do, and what it may use."""

    goal: str
    settings: Settings = field(default_factory=get_settings)
    conversation_id: int | None = None
    task_id: int | None = None
    project_id: int | None = None
    project_instructions: str = ""
    # Findings passed down from earlier agents (plan, diff, review …).
    inputs: dict[str, Any] = field(default_factory=dict)
    history: list[Message] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    cancel: Any = None
    depth: int = 0

    def child(self, goal: str, **inputs: Any) -> AgentContext:
        return AgentContext(
            goal=goal,
            settings=self.settings,
            conversation_id=self.conversation_id,
            task_id=self.task_id,
            project_id=self.project_id,
            project_instructions=self.project_instructions,
            inputs={**self.inputs, **inputs},
            memories=self.memories,
            cancel=self.cancel,
            depth=self.depth + 1,
        )

    def check_cancelled(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("Der Vorgang wurde abgebrochen.")


@dataclass
class AgentResult:
    agent: str
    text: str = ""
    ok: bool = True
    artifacts: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[str] = field(default_factory=list)
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    duration_ms: int = 0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "ok": self.ok,
            "text": self.text,
            "artifacts": self.artifacts,
            "tools": self.tool_calls,
            "model": self.model,
            "usage": self.usage.to_dict(),
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


class Agent(abc.ABC):
    """Base class for every specialist."""

    name: str = "agent"
    role: str = ""
    task_kind: str = "chat"          # which routing profile the model router should use
    tool_tags: set[str] = set()      # which tools this agent may use
    needs_tools: bool = False
    needs_vision: bool = False
    needs_reasoning: bool = False
    max_output_tokens: int | None = None

    def __init__(
        self,
        *,
        model_router: ModelRouter | None = None,
        tool_registry: Any = None,
    ) -> None:
        self._router = model_router or router
        self._registry = tool_registry

    # --- subclass surface --------------------------------------------------------------

    @abc.abstractmethod
    def system_prompt(self, context: AgentContext) -> str:
        """The agent's instructions. Should state its role and what it must not do."""

    def user_prompt(self, context: AgentContext) -> str:
        return context.goal

    async def run(self, context: AgentContext) -> AgentResult:
        """Default behaviour: one model call plus a bounded tool loop."""
        started = time.perf_counter()
        event_bus.emit(EventType.AGENT_STARTED, agent=self.name, goal=context.goal[:200])
        try:
            result = await self._converse(context)
        except Cancelled:
            raise
        except JarvisError as exc:
            log.warning("Agent %s fehlgeschlagen: %s", self.name, exc)
            result = AgentResult(self.name, ok=False, error=str(exc), text=exc.user_message)
        except Exception as exc:
            log.exception("Agent %s ist unerwartet fehlgeschlagen", self.name)
            result = AgentResult(self.name, ok=False, error=f"{type(exc).__name__}: {exc}",
                                 text=f"{self.name} ist unerwartet fehlgeschlagen.")
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        event_bus.emit(EventType.AGENT_COMPLETED, agent=self.name, ok=result.ok,
                       duration_ms=result.duration_ms)
        return result

    # --- shared machinery ---------------------------------------------------------------

    def requirements(self) -> Requirements:
        return Requirements(
            needs_tools=self.needs_tools,
            needs_vision=self.needs_vision,
            needs_reasoning=self.needs_reasoning,
        )

    async def select_model(self, context: AgentContext) -> ModelSelection:
        return await self._router.select(
            self.task_kind, requirements=self.requirements(), settings=context.settings
        )

    def tools_for(self, context: AgentContext) -> list[ToolSpec]:
        if self._registry is None or not self.tool_tags:
            return []
        specs: list[ToolSpec] = []
        for tool in self._registry.all():
            tags = self._registry._tags.get(tool.name, set())   # noqa: SLF001 - same package
            if not (tags & self.tool_tags):
                continue
            if context.settings.permission_for(tool.required_permission).value == "DENY":
                continue
            specs.append(tool.to_spec())
        return specs

    def build_messages(
        self, context: AgentContext, selection: ModelSelection, extra_sections: Sequence[tuple[str, str]] = ()
    ) -> list[Message]:
        builder = ContextBuilder(
            selection.model.context_length,
            output_reserve=self.max_output_tokens or context.settings.models.max_output_tokens,
        )
        builder.add_memories(context.memories)
        if context.project_instructions:
            builder.add(Priority.PROJECT_INSTRUCTIONS, "Projekt", context.project_instructions)
        for label, content in extra_sections:
            builder.add(Priority.CURRENT_TASK, label, content)
        for key, value in context.inputs.items():
            if isinstance(value, str) and value.strip():
                builder.add(Priority.CURRENT_TASK, key, value)

        built = builder.build(
            system_prompt=self.system_prompt(context),
            user_message=self.user_prompt(context),
            history=context.history,
        )
        return built.messages

    async def _converse(self, context: AgentContext) -> AgentResult:
        context.check_cancelled()
        selection = await self.select_model(context)
        tools = self.tools_for(context)
        if tools and selection.model.supports_tools is not True and not selection.model.is_router:
            tools = []

        messages = self.build_messages(context, selection)
        result = AgentResult(self.name, model=selection.model.key)
        parts: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            context.check_cancelled()
            event_bus.emit(EventType.AGENT_STATUS, agent=self.name, status="denkt nach",
                           model=selection.model.key)
            response: ChatResult = await selection.provider.chat(
                messages,
                selection.model.id,
                tools=tools or None,
                temperature=context.settings.models.temperature,
                max_tokens=self.max_output_tokens or context.settings.models.max_output_tokens,
            )
            result.usage = _add(result.usage, response.usage)
            if response.text:
                parts.append(response.text)

            if not response.tool_calls:
                break

            messages.append(Message("assistant", response.text, tool_calls=response.tool_calls,
                                    trust="system"))
            for call in response.tool_calls:
                outcome = await self._run_tool(call, context)
                result.tool_calls.append(call.name)
                messages.append(Message("tool", outcome, name=call.name,
                                        tool_call_id=call.id, trust="tool_result"))
        else:
            parts.append(
                f"\n[{self.name} hat nach {MAX_TOOL_ROUNDS} Werkzeugrunden abgebrochen.]"
            )

        result.text = "\n".join(part for part in parts if part).strip()
        return result

    async def _run_tool(self, call: ToolCall, context: AgentContext) -> str:
        if self._registry is None:
            return wrap_tool_result(call.name, "Es ist kein Werkzeug verfügbar.")
        from tools.base import ToolContext

        tool_context = ToolContext(
            conversation_id=context.conversation_id,
            task_id=context.task_id,
            agent=self.name,
            cancel=context.cancel,
        )
        event_bus.emit(EventType.AGENT_STATUS, agent=self.name,
                       status=f"nutzt {call.name}", tool=call.name)
        result = await self._registry.execute(
            call.name, call.arguments, context=tool_context, settings=context.settings,
            call_id=call.id,
        )
        return wrap_tool_result(call.name, result.content)


def _add(left: Usage, right: Usage) -> Usage:
    def add(a: int | None, b: int | None) -> int | None:
        return None if a is None and b is None else (a or 0) + (b or 0)

    return Usage(
        prompt_tokens=add(left.prompt_tokens, right.prompt_tokens),
        completion_tokens=add(left.completion_tokens, right.completion_tokens),
        total_tokens=add(left.total_tokens, right.total_tokens),
        cost_usd=None if left.cost_usd is None and right.cost_usd is None
        else (left.cost_usd or 0.0) + (right.cost_usd or 0.0),
    )
