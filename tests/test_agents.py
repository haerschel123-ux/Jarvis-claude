"""Agent system: escalation, workflow and honesty (Spec §29-§34, §44, §100, §101)."""

from __future__ import annotations

import pytest

from agents.base import Agent, AgentContext, AgentResult
from agents.coding import CodingAgent
from agents.coordinator import Coordinator, Strategy
from agents.orchestrator import Orchestrator
from agents.planner import parse_plan, render_plan
from agents.research import ResearchAgent
from agents.review import parse_review
from core.config import Settings
from core.enums import CodingMode
from core.intent import classify_rules
from core.model_router import ModelRouter
from core.tasks import TaskManager
from memory.database import Database
from providers.catalog import ModelCatalog
from tests.fakes import ScriptedProvider, free_model, text_turn

CHAT_MODEL = free_model("free/chat", supports_tools=True, context_length=32768)
OTHER_MODEL = free_model("free/second", supports_tools=True, context_length=32768)


# --- coordinator escalation --------------------------------------------------------------


@pytest.fixture
def coordinator() -> Coordinator:
    return Coordinator()


def decide(coordinator: Coordinator, message: str, settings: Settings | None = None):
    return coordinator.decide(message, classify_rules(message), settings or Settings())


@pytest.mark.parametrize(
    "message",
    [
        "Hallo Jarvis", "Danke!", "Wie geht's?", "Guten Morgen",
        "Wie viele Einwohner hat Berlin?",
    ],
)
def test_trivial_messages_never_start_an_agent(coordinator: Coordinator, message: str) -> None:
    """Spec §30: five agents must not start for every little thing."""
    assert decide(coordinator, message).strategy is Strategy.DIRECT


@pytest.mark.parametrize(
    "message",
    [
        "Öffne Discord.", "Jarvis, starte Visual Studio Code.", "Schließe den Browser.",
        "Erinnere mich morgen um 18 Uhr an den Server.",
        "Merke dir, dass ich Tabs bevorzuge.",
        "Lies die Datei D:/Projects/notes.txt.",
    ],
)
def test_single_actions_use_one_tool(coordinator: Coordinator, message: str) -> None:
    assert decide(coordinator, message).strategy is Strategy.SINGLE_TOOL


def test_a_program_name_that_is_also_a_domain_stays_a_single_tool(coordinator: Coordinator) -> None:
    """"Öffne Discord" is launching a program, not Discord-bot work."""
    assert decide(coordinator, "Öffne Discord.").strategy is Strategy.SINGLE_TOOL
    # But actual Discord work does reach the specialist.
    assert decide(coordinator,
                  "Prüfe meinen Discord Bot und sieh dir die Logs an.").strategy \
        is Strategy.SPECIALIST


@pytest.mark.parametrize(
    ("message", "agent"),
    [
        ("Suche im Internet nach der aktuellen faster-whisper Version.", "research"),
        ("Repariere den Fehler in meinem Python Skript.", "coding"),
        ("Mach einen Screenshot und sag mir was du siehst.", "computer"),
        ("Prüfe meine events.xml auf Fehler.", "dayz"),
    ],
)
def test_domain_work_goes_to_its_specialist(coordinator: Coordinator, message: str, agent: str) -> None:
    decision = decide(coordinator, message)
    assert decision.strategy is Strategy.SPECIALIST
    assert decision.agents[0] == agent


def test_multi_step_work_gets_the_full_workflow(coordinator: Coordinator) -> None:
    decision = decide(
        coordinator,
        "Prüfe meinen DayZ Server und repariere alle Fehler in der events.xml, "
        "danach lade sie hoch.",
    )
    assert decision.strategy is Strategy.WORKFLOW
    assert decision.agents[0] == "planner"


def test_consequential_work_is_not_waved_through_as_small_talk(coordinator: Coordinator) -> None:
    """A request the classifier is unsure about must not skip the safeguards."""
    decision = decide(coordinator, "Migriere die Datenbank auf dem Produktivserver.")
    assert decision.strategy is Strategy.WORKFLOW


def test_second_opinion_only_when_enabled_and_warranted(coordinator: Coordinator) -> None:
    """Spec §32: no artificial debate for trivial tasks."""
    settings = Settings()
    settings.models.multi_agent_review = True

    trivial = decide(coordinator, "Lies die Datei D:/a.txt.", settings)
    assert trivial.review is False

    critical = decide(coordinator, "Ändere die Authentifizierung im Produktivsystem.", settings)
    assert critical.review is True

    settings.models.multi_agent_review = False
    assert decide(coordinator, "Ändere die Authentifizierung im Produktivsystem.",
                  settings).review is False


def test_without_tools_everything_is_answered_directly(coordinator: Coordinator) -> None:
    decision = coordinator.decide(
        "Repariere den Fehler in meinem Skript.",
        classify_rules("Repariere den Fehler in meinem Skript."),
        Settings(),
        has_tools=False,
    )
    assert decision.strategy is Strategy.DIRECT


def test_the_decision_can_be_explained(coordinator: Coordinator) -> None:
    for message in ("Hallo", "Öffne Discord.", "Repariere den Bug in main.py.",
                    "Prüfe alles und repariere dann die Fehler, danach committe."):
        assert coordinator.explain(decide(coordinator, message))


# --- planner and review parsing ------------------------------------------------------------


def test_planner_output_is_parsed_and_rendered() -> None:
    raw = ('Gerne. {"plan": [{"title": "Datei lesen", "detail": "events.xml einlesen", '
           '"done_when": "Inhalt liegt vor"}], "risks": ["Datei fehlt"], '
           '"needs_confirmation": false}')
    plan = parse_plan(raw)
    assert len(plan["plan"]) == 1
    assert plan["risks"] == ["Datei fehlt"]
    assert "Fertig, wenn" in render_plan(plan)


def test_planner_falls_back_to_a_numbered_list() -> None:
    plan = parse_plan("1. Erst lesen\n2. Dann prüfen\n3. Dann berichten")
    assert len(plan["plan"]) == 3


def test_an_unusable_plan_is_empty_not_invented() -> None:
    assert parse_plan("Ich weiß es nicht.")["plan"] == []


def test_a_blocker_overrides_an_ok_verdict() -> None:
    """A reviewer that says "ok" while listing a blocker must not count as approval."""
    verdict = parse_review(
        '{"verdict": "ok", "summary": "Passt", "issues": '
        '[{"severity": "blocker", "where": "a.py", "problem": "Pfad ungeprüft"}]}'
    )
    assert verdict["verdict"] == "needs_changes"
    assert len(verdict["blockers"]) == 1


def test_an_unparseable_review_is_marked_as_such() -> None:
    verdict = parse_review("völlig unstrukturierte Antwort")
    assert verdict["structured"] is False


# --- coding agent modes -----------------------------------------------------------------------


@pytest.fixture
async def wired(database: Database):
    provider = ScriptedProvider([CHAT_MODEL, OTHER_MODEL])
    catalog = ModelCatalog(database)
    catalog._providers = {"scripted": provider}              # noqa: SLF001
    await catalog.refresh(force=True)

    from tools import register_default_tools
    from tools.registry import ToolRegistry

    registry = ToolRegistry(database)
    register_default_tools(registry)

    import providers.catalog as catalog_module

    original = catalog_module.catalog
    catalog_module.catalog = catalog
    try:
        yield ModelRouter(catalog), registry, provider, TaskManager(database)
    finally:
        catalog_module.catalog = original


def coding_settings(mode: CodingMode) -> Settings:
    settings = Settings()
    settings.assistant.coding_mode = mode
    settings.security.trusted_folders = ["/tmp"]
    return settings


@pytest.mark.parametrize("mode", [CodingMode.CHAT, CodingMode.PLAN_ONLY])
async def test_read_only_coding_modes_are_offered_no_writing_tools(wired, mode: CodingMode) -> None:
    """Spec §34: the prompt alone would leave a model free to ignore the mode."""
    model_router, registry, _, _ = wired
    agent = CodingAgent(model_router=model_router, tool_registry=registry)
    context = AgentContext(goal="Sieh dir main.py an", settings=coding_settings(mode))
    names = {spec.name for spec in agent.tools_for(context)}
    assert "read_file" in names
    assert not names & {"write_file", "patch_file", "delete_file", "run_command"}


async def test_editing_modes_get_writing_tools(wired) -> None:
    model_router, registry, _, _ = wired
    agent = CodingAgent(model_router=model_router, tool_registry=registry)
    context = AgentContext(goal="Repariere main.py",
                           settings=coding_settings(CodingMode.AUTO_EDIT))
    names = {spec.name for spec in agent.tools_for(context)}
    assert {"write_file", "patch_file"} <= names


async def test_the_coding_mode_is_stated_in_the_prompt(wired) -> None:
    model_router, registry, _, _ = wired
    agent = CodingAgent(model_router=model_router, tool_registry=registry)
    prompt = agent.system_prompt(AgentContext(goal="x", settings=coding_settings(CodingMode.CHAT)))
    assert "NICHTS ändern" in prompt
    assert "Erfinde niemals" in prompt


# --- research honesty -----------------------------------------------------------------------------


async def test_research_without_web_tools_says_so_instead_of_pretending(wired) -> None:
    """Spec §44: never claim to have researched when no search ran."""
    model_router, registry, _, _ = wired
    from core.enums import PermissionValue

    settings = Settings()
    settings.permissions["web_search"] = PermissionValue.DENY
    agent = ResearchAgent(model_router=model_router, tool_registry=registry)
    result = await agent.run(AgentContext(goal="Was ist neu?", settings=settings))
    assert result.ok is False
    assert "keine Recherche" in result.text or "nicht recherchieren" in result.text


async def test_research_marks_an_answer_that_used_no_search(wired) -> None:
    model_router, registry, provider, _ = wired
    provider._script = [text_turn("Die Version ist 1.2.3.")]      # noqa: SLF001
    settings = Settings()
    settings.security.trusted_folders = ["/tmp"]
    agent = ResearchAgent(model_router=model_router, tool_registry=registry)
    result = await agent.run(AgentContext(goal="Welche Version?", settings=settings))
    assert result.artifacts["searched"] is False
    assert "Modellwissen" in result.text


# --- orchestration ------------------------------------------------------------------------------------


class StubAgent(Agent):
    """An agent that returns a fixed answer without touching a model."""

    def __init__(self, name: str, text: str = "erledigt", ok: bool = True, **kwargs) -> None:
        super().__init__(**kwargs)
        self.name = name
        self._text = text
        self._ok = ok
        self.runs = 0

    def system_prompt(self, context: AgentContext) -> str:
        return "stub"

    async def run(self, context: AgentContext) -> AgentResult:
        self.runs += 1
        return AgentResult(self.name, text=self._text, ok=self._ok, model="stub/model")


async def test_direct_strategy_starts_no_agent(wired) -> None:
    model_router, registry, _, task_manager = wired
    orchestrator = Orchestrator(model_router=model_router, tool_registry=registry,
                                task_manager=task_manager)
    decision = orchestrator.decide("Hallo", classify_rules("Hallo"), Settings())
    result = await orchestrator.run(decision, AgentContext(goal="Hallo"))
    assert result.steps == []
    assert result.task_id is None


async def test_a_workflow_runs_planner_then_executor_and_records_a_task(wired, monkeypatch) -> None:
    model_router, registry, _, task_manager = wired
    orchestrator = Orchestrator(model_router=model_router, tool_registry=registry,
                                task_manager=task_manager)

    planner = StubAgent("planner", "Plan:\n1. Etwas tun")
    executor = StubAgent("coding", "Umgesetzt.")
    built: list[str] = []

    def fake_build(name: str, **kwargs):
        built.append(name)
        return {"planner": planner, "coding": executor, "executor": executor,
                "review": StubAgent("review", "Prüfung: In Ordnung")}.get(name)

    monkeypatch.setattr(orchestrator, "_build", fake_build)

    message = "Prüfe alles und repariere dann die Fehler, danach committe."
    decision = orchestrator.decide(message, classify_rules(message), Settings())
    assert decision.strategy is Strategy.WORKFLOW

    result = await orchestrator.run(decision, AgentContext(goal=message))
    assert result.ok is True
    # The generic executor slot is filled by the coding agent today.
    assert built[:2] == ["planner", "executor"]
    assert planner.runs == 1 and executor.runs == 1
    assert "Plan" in result.text and "Umsetzung" in result.text

    task = await task_manager.get(result.task_id)
    assert task["status"] == "DONE"


async def test_a_failing_planner_stops_the_workflow(wired, monkeypatch) -> None:
    """A broken plan must not be carried forward into an execution step."""
    model_router, registry, _, task_manager = wired
    orchestrator = Orchestrator(model_router=model_router, tool_registry=registry,
                                task_manager=task_manager)
    executor = StubAgent("coding")
    monkeypatch.setattr(orchestrator, "_build", lambda name, **kwargs: {
        "planner": StubAgent("planner", "kein Plan", ok=False),
        "coding": executor, "executor": executor,
    }.get(name))

    message = "Erst dies und dann das, danach committe."
    decision = orchestrator.decide(message, classify_rules(message), Settings())
    result = await orchestrator.run(decision, AgentContext(goal=message))
    assert result.ok is False
    assert executor.runs == 0


async def test_a_review_with_blockers_triggers_exactly_one_repair(wired, monkeypatch) -> None:
    """Spec §101: repair, but never in an endless loop."""
    model_router, registry, _, task_manager = wired
    orchestrator = Orchestrator(model_router=model_router, tool_registry=registry,
                                task_manager=task_manager)

    executor = StubAgent("coding", "Umgesetzt.")

    class BlockingReviewer(StubAgent):
        async def run(self, context: AgentContext) -> AgentResult:
            self.runs += 1
            return AgentResult(
                "review", text="Prüfung: Nachbesserung nötig", ok=True,
                artifacts={"verdict": "needs_changes",
                           "blockers": [{"severity": "blocker", "problem": "x"}]},
            )

    reviewer = BlockingReviewer("review")
    monkeypatch.setattr(orchestrator, "_build", lambda name, **kwargs: {
        "planner": StubAgent("planner", "Plan:\n1. X"),
        "coding": executor, "executor": executor, "review": reviewer,
    }.get(name))

    settings = Settings()
    settings.models.multi_agent_review = True
    message = "Ändere die Authentifizierung im Produktivsystem und deploye danach."
    decision = orchestrator.decide(message, classify_rules(message), settings)
    await orchestrator.run(decision, AgentContext(goal=message, settings=settings))

    assert reviewer.runs == 1
    assert executor.runs == 2        # the original run plus exactly one repair


async def test_an_unknown_agent_is_reported_not_silently_skipped(wired) -> None:
    model_router, registry, _, task_manager = wired
    orchestrator = Orchestrator(model_router=model_router, tool_registry=registry,
                                task_manager=task_manager)
    from agents.coordinator import Decision

    decision = Decision(Strategy.SPECIALIST, ["gibtsnicht"], "test")
    result = await orchestrator.run(decision, AgentContext(goal="test"))
    assert result.ok is False
    assert "nicht verfügbar" in result.text
