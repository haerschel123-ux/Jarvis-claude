"""Agent orchestration (Spec §30, §31, §32, §100).

Runs the strategy the coordinator chose:

``DIRECT`` / ``SINGLE_TOOL``  handled by the assistant core itself — no agent starts
``SPECIALIST``                one agent, optionally followed by a review
``WORKFLOW``                  planner → executor → review, with one repair round

The workflow stops at the first hard failure rather than carrying a broken result forward,
and a review that finds blockers triggers exactly one repair attempt — not an endless loop
(Spec §101).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from agents.base import Agent, AgentContext, AgentResult
from agents.coding import CodingAgent
from agents.coordinator import Decision, Strategy, coordinator
from agents.planner import PlannerAgent
from agents.research import ResearchAgent
from agents.review import ReviewAgent
from core.config import Settings
from core.errors import Cancelled
from core.events import EventType, event_bus
from core.intent import IntentResult
from core.logging_setup import get_logger
from core.model_router import ModelRouter, router
from core.tasks import TaskManager, tasks

log = get_logger("agents.orchestrator")

MAX_REPAIR_ROUNDS = 1


@dataclass
class WorkflowResult:
    ok: bool
    text: str
    decision: Decision
    steps: list[AgentResult] = field(default_factory=list)
    task_id: int | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "decision": self.decision.to_dict(),
            "steps": [step.to_dict() for step in self.steps],
            "task_id": self.task_id,
            "duration_ms": self.duration_ms,
        }


class Orchestrator:
    def __init__(
        self,
        *,
        model_router: ModelRouter | None = None,
        tool_registry: Any = None,
        task_manager: TaskManager | None = None,
    ) -> None:
        self._router = model_router or router
        self._registry = tool_registry
        self._tasks = task_manager or tasks
        self._extra: dict[str, type[Agent]] = {}

    def register_agent(self, name: str, agent_class: type[Agent]) -> None:
        """Let later stages add specialists (computer, dayz, discord, github)."""
        self._extra[name] = agent_class

    def available_agents(self) -> list[str]:
        return sorted({"planner", "coding", "research", "review", *self._extra})

    def _build(self, name: str, **kwargs: Any) -> Agent | None:
        builtin: dict[str, type[Agent]] = {
            "planner": PlannerAgent,
            "coding": CodingAgent,
            "executor": CodingAgent,     # the general executor is the coding agent today
            "research": ResearchAgent,
            "review": ReviewAgent,
        }
        agent_class = self._extra.get(name) or builtin.get(name)
        if agent_class is None:
            log.warning("Unbekannter Agent '%s'", name)
            return None
        return agent_class(model_router=self._router, tool_registry=self._registry, **kwargs)

    # --- entry point ----------------------------------------------------------------

    def decide(
        self, message: str, intent: IntentResult, settings: Settings, *, has_tools: bool = True
    ) -> Decision:
        decision = coordinator.decide(message, intent, settings, has_tools=has_tools)
        coordinator.announce(decision)
        return decision

    async def run(self, decision: Decision, context: AgentContext) -> WorkflowResult:
        """Execute the chosen strategy."""
        started = time.perf_counter()
        if decision.strategy in (Strategy.DIRECT, Strategy.SINGLE_TOOL):
            return WorkflowResult(True, "", decision)

        task_id = await self._tasks.create(
            _task_title(context.goal),
            goal=context.goal,
            conversation_id=context.conversation_id,
            project_id=context.project_id,
            agent=decision.agents[0] if decision.agents else "coordinator",
        )
        context.task_id = task_id
        await self._tasks.start(task_id)
        handle = self._tasks.handle(task_id)
        if handle is not None and context.cancel is None:
            context.cancel = handle.cancel

        try:
            if decision.strategy is Strategy.SPECIALIST:
                result = await self._run_specialist(decision, context)
            else:
                result = await self._run_workflow(decision, context)
        except Cancelled:
            await self._tasks.cancel(task_id, "abgebrochen")
            return WorkflowResult(False, "Der Vorgang wurde abgebrochen.", decision,
                                  task_id=task_id)
        except Exception as exc:
            log.exception("Agentenlauf fehlgeschlagen")
            await self._tasks.fail(task_id, f"{type(exc).__name__}: {exc}")
            return WorkflowResult(False, f"Der Agentenlauf ist fehlgeschlagen: {type(exc).__name__}",
                                  decision, task_id=task_id)

        result.task_id = task_id
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        if result.ok:
            await self._tasks.complete(task_id, result.text[:2000])
        else:
            await self._tasks.fail(task_id, result.text[:2000])
        return result

    # --- strategies ------------------------------------------------------------------

    async def _run_specialist(self, decision: Decision, context: AgentContext) -> WorkflowResult:
        name = decision.agents[0]
        agent = self._build(name)
        if agent is None:
            return WorkflowResult(
                False, f"Der Agent '{name}' ist nicht verfügbar.", decision
            )

        step_ids = await self._tasks.add_steps(context.task_id, [f"{name} ausführen"])
        result = await agent.run(context)
        await self._finish_step(step_ids[0], result)

        steps = [result]
        if result.ok and decision.review:
            review = await self._review(context, result, steps)
            if review is not None and review.artifacts.get("blockers"):
                repaired = await self._repair(agent, context, result, review, steps)
                if repaired is not None:
                    result = repaired

        return WorkflowResult(result.ok, _combine(steps), decision, steps)

    async def _run_workflow(self, decision: Decision, context: AgentContext) -> WorkflowResult:
        steps: list[AgentResult] = []

        planner = self._build("planner")
        if planner is None:
            return WorkflowResult(False, "Der Planer ist nicht verfügbar.", decision)

        plan_step, exec_step = await self._tasks.add_steps(
            context.task_id, ["Plan erstellen", "Plan ausführen"]
        )

        event_bus.emit(EventType.AGENT_STATUS, agent="coordinator", status="Planung läuft")
        plan_result = await planner.run(context)
        steps.append(plan_result)
        await self._finish_step(plan_step, plan_result)
        if not plan_result.ok:
            return WorkflowResult(False, _combine(steps), decision, steps)

        # The plan's individual steps are deliberately NOT added as task steps: the executor
        # carries out the whole plan in one go, so JARVIS cannot honestly report which single
        # step succeeded. Showing them as permanently "pending" on a finished task would be
        # misleading. The plan itself is in the planner's result and in the answer.

        executor_name = next((a for a in decision.agents if a not in ("planner", "review")),
                             "coding")
        executor = self._build(executor_name)
        if executor is None:
            return WorkflowResult(False, f"Der Agent '{executor_name}' ist nicht verfügbar.",
                                  decision, steps)

        exec_context = context.child(context.goal, plan=plan_result.text)
        exec_context.task_id = context.task_id
        exec_result = await executor.run(exec_context)
        steps.append(exec_result)
        await self._finish_step(exec_step, exec_result)
        if not exec_result.ok:
            return WorkflowResult(False, _combine(steps), decision, steps)

        if "review" in decision.agents:
            review = await self._review(context, exec_result, steps, plan=plan_result.text)
            if review is not None and review.artifacts.get("blockers"):
                repaired = await self._repair(executor, context, exec_result, review, steps,
                                              plan=plan_result.text)
                if repaired is not None:
                    exec_result = repaired

        return WorkflowResult(exec_result.ok, _combine(steps), decision, steps)

    async def _review(
        self,
        context: AgentContext,
        work: AgentResult,
        steps: list[AgentResult],
        *,
        plan: str = "",
    ) -> AgentResult | None:
        # Ask for a different model than the one under review (Spec §32).
        reviewer = self._build("review", avoid_model=work.model)
        if reviewer is None:
            return None
        step_id = (await self._tasks.add_steps(context.task_id, ["Ergebnis prüfen"]))[0]
        review_context = context.child(
            context.goal,
            work=work.text,
            plan=plan,
            changed_files=", ".join(work.artifacts.get("changed_files", [])),
        )
        review_context.task_id = context.task_id
        result = await reviewer.run(review_context)
        steps.append(result)
        await self._finish_step(step_id, result)
        return result

    async def _repair(
        self,
        executor: Agent,
        context: AgentContext,
        work: AgentResult,
        review: AgentResult,
        steps: list[AgentResult],
        *,
        plan: str = "",
    ) -> AgentResult | None:
        """One repair round. Not a loop — repeating forever is worse than reporting (Spec §101)."""
        for _ in range(MAX_REPAIR_ROUNDS):
            step_id = (await self._tasks.add_steps(context.task_id, ["Anmerkungen umsetzen"]))[0]
            repair_context = context.child(context.goal, plan=plan, review=review.text)
            repair_context.task_id = context.task_id
            result = await executor.run(repair_context)
            steps.append(result)
            await self._finish_step(step_id, result)
            return result
        return None

    async def _finish_step(self, step_id: int, result: AgentResult) -> None:
        from core.enums import TaskStatus

        await self._tasks.update_step(
            step_id,
            TaskStatus.DONE if result.ok else TaskStatus.FAILED,
            result=result.text[:1000],
            detail=f"{result.agent} · {result.model} · {result.duration_ms} ms",
        )


def _combine(steps: list[AgentResult]) -> str:
    """Join the agents' output into one answer, labelled by who said what."""
    labels = {
        "planner": "Plan", "coding": "Umsetzung", "executor": "Umsetzung",
        "research": "Recherche", "review": "Prüfung",
    }
    parts: list[str] = []
    for step in steps:
        if not step.text.strip():
            continue
        label = labels.get(step.agent, step.agent)
        parts.append(f"**{label}**\n{step.text.strip()}")
    return "\n\n".join(parts)


def _task_title(goal: str) -> str:
    text = " ".join((goal or "Aufgabe").split())
    return text[:70] + (" …" if len(text) > 70 else "")


orchestrator = Orchestrator()
