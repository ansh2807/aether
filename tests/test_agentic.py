"""The agentic view: staffing a company of personas onto AETHER's models.

Three layers, deliberately independent — role (what kind of work), persona (who
does it), provider (which model runs them). A persona surviving its provider's
failure is the whole reason they are not the same thing.

The roster is external and optional, so these tests build a small one on disk
rather than depending on the user's. A test that needs 271 real personas to
pass would be untestable in CI and would break whenever the roster changed.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aether.agentic.roster import Roster
from aether.agentic.staffing import persona_preamble, staff_subtask, staff_task
from aether.domain.enums import AgentRole
from aether.domain.models import Subtask

ROSTER = [
    ("security-appsec", "Application Security Engineer", "security",
     "Secures the software supply chain, reviews auth flows and payment handling"),
    ("security-blockchain", "Blockchain Security Auditor", "security",
     "Audits smart contracts and on-chain payment systems"),
    ("engineering-backend", "Backend Architect", "engineering",
     "Designs services, APIs and data models"),
    ("engineering-database", "Database Optimizer", "engineering",
     "Tunes schemas, indexes and slow queries"),
    ("testing-qa", "QA Engineer", "testing",
     "Writes end-to-end and regression test suites"),
    ("marketing-content", "Content Creator", "marketing",
     "Writes landing page copy, launch announcements and product narrative"),
    ("marketing-healthcare", "Healthcare Marketing Compliance Specialist", "marketing",
     "Ensures medical marketing meets regulatory requirements"),
]


@pytest.fixture
def roster(tmp_path: Path) -> Roster:
    (tmp_path / ".claude" / "agents").mkdir(parents=True)
    (tmp_path / "digital-wizards-roster.json").write_text(
        json.dumps([
            {"slug": s, "name": n, "division": d, "description": desc}
            for s, n, d, desc in ROSTER
        ]),
        encoding="utf-8",
    )
    for slug, name, _, desc in ROSTER:
        (tmp_path / ".claude" / "agents" / f"{slug}.md").write_text(
            f"---\nname: {name}\ndescription: {desc}\n---\n\n"
            f"# {name}\n\nYou are **{name}**. {desc}\n",
            encoding="utf-8",
        )
    return Roster(tmp_path)


def _sub(title: str, role: AgentRole, description: str = "") -> Subtask:
    return Subtask(task_id="T", title=title, description=description, role=role)


# --- loading -------------------------------------------------------------


def test_the_roster_loads_and_indexes_by_division(roster: Roster):
    assert len(roster) == len(ROSTER)
    assert roster.division_counts()["security"] == 2
    assert [p.name for p in roster.by_division("security")] == [
        "Application Security Engineer", "Blockchain Security Auditor",
    ]


def test_a_missing_roster_is_not_an_error(tmp_path: Path):
    """AETHER works exactly as before without one; absence must not raise."""
    assert Roster.discover(tmp_path / "nope") is None


def test_a_corrupt_index_degrades_to_empty(tmp_path: Path):
    (tmp_path / "digital-wizards-roster.json").write_text("{ not json", encoding="utf-8")
    assert len(Roster(tmp_path)) == 0


def test_persona_prompts_are_loaded_lazily(roster: Roster):
    """A roster can hold hundreds of agents; reading every system prompt to
    staff three of them would be waste."""
    persona = roster.get("security-appsec")
    assert persona._prompt is None, "the index eagerly read a prompt body"
    assert "Application Security Engineer" in persona.load_prompt()


def test_frontmatter_is_stripped_from_the_prompt(roster: Roster):
    prompt = roster.get("engineering-backend").load_prompt()
    assert not prompt.startswith("---"), "YAML frontmatter leaked into the system prompt"
    assert "name: Backend Architect" not in prompt


def test_a_persona_whose_file_vanished_does_not_crash(roster: Roster):
    """The index and the definition files can drift apart."""
    persona = roster.get("testing-qa")
    persona.source.unlink()
    assert persona.load_prompt() == ""


def test_a_long_prompt_is_truncated(roster: Roster):
    """Persona prompts compete with the task's own context for the window."""
    persona = roster.get("engineering-backend")
    persona.source.write_text("---\nname: x\n---\n" + ("word " * 20_000), encoding="utf-8")
    assert len(persona.load_prompt(max_chars=1000)) < 1200


# --- search --------------------------------------------------------------


def test_search_matches_any_word_not_the_whole_phrase(roster: Roster):
    """`who "payment security"` returned nothing when this was a substring
    match — no single persona contains that exact phrase."""
    hits = roster.search("payment security")
    assert hits, "a multi-word query found nobody"
    assert {p.division for p in hits} & {"security"}


def test_search_ranks_multi_word_matches_first(roster: Roster):
    hits = roster.search("blockchain payment")
    assert hits[0].slug == "security-blockchain"


# --- staffing ------------------------------------------------------------


def test_work_goes_to_the_right_division(roster: Roster):
    """The dependable property. Which individual is approximate; the division
    should not be."""
    cases = [
        ("Audit the auth flow for security holes", AgentRole.SECURITY_REVIEWER, "security"),
        ("Write end-to-end tests for checkout", AgentRole.TESTER, "testing"),
        ("Design the database schema", AgentRole.ARCHITECT, "engineering"),
        ("Write the marketing landing page copy", AgentRole.IMPLEMENTER, "marketing"),
    ]
    for title, role, expected in cases:
        got = staff_subtask(roster, _sub(title, role))
        assert got.persona is not None, f"nobody staffed for {title!r}"
        assert got.persona.division == expected, (
            f"{title!r} went to {got.persona.name} ({got.persona.division}), "
            f"expected the {expected} division"
        )


def test_the_goal_informs_staffing_but_does_not_decide_it():
    """The bug this pins: goal terms were weighted like subtask terms, so every
    subtask of "build a multiplayer game..." matched on "game" — the marketing
    copy went to a Game Audio Engineer."""
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    (tmp / ".claude" / "agents").mkdir(parents=True)
    (tmp / "digital-wizards-roster.json").write_text(json.dumps([
        {"slug": "g", "name": "Game Audio Engineer", "division": "game-development",
         "description": "Sound design for games"},
        {"slug": "m", "name": "Content Creator", "division": "marketing",
         "description": "Writes landing page copy and launch announcements"},
    ]), encoding="utf-8")
    r = Roster(tmp)

    got = staff_subtask(r, _sub("Write the marketing landing page copy",
                                AgentRole.IMPLEMENTER),
                        goal="Build a multiplayer game with a marketing launch page")
    assert got.persona.name == "Content Creator", (
        f"the goal's 'game' outweighed the subtask; got {got.persona.name}"
    )


def test_an_unasked_specialisation_counts_against_a_persona(roster: Roster):
    """"Healthcare Marketing Compliance Specialist" won landing-page copy on the
    single word "marketing". A narrow expert should win only when their
    narrowness was requested."""
    got = staff_subtask(roster, _sub("Write the marketing landing page copy",
                                     AgentRole.IMPLEMENTER))
    assert got.persona.name == "Content Creator"

    # ...and is still reachable when it *is* what was asked for.
    healthcare = staff_subtask(
        roster, _sub("Review healthcare marketing copy for compliance", AgentRole.REVIEWER)
    )
    assert healthcare.persona.name == "Healthcare Marketing Compliance Specialist"


def test_every_assignment_explains_itself(roster: Roster):
    """The plan is shown for approval, so every row needs a why."""
    got = staff_subtask(roster, _sub("Audit the payment flow", AgentRole.SECURITY_REVIEWER))
    assert got.reasons, "an assignment with no stated reason cannot be reviewed"
    assert got.score > 0


def test_staffing_is_deterministic(roster: Roster):
    """Reproducibility is the reason this is not an LLM call."""
    sub = _sub("Design the database schema", AgentRole.ARCHITECT)
    runs = {staff_subtask(roster, sub).persona.slug for _ in range(5)}
    assert len(runs) == 1


def test_an_unmatchable_subtask_is_left_unstaffed_not_forced(roster: Roster):
    """Assigning a random specialist would be worse than admitting no match:
    the persona preamble would actively mislead the model."""
    got = staff_subtask(roster, _sub("zzzz", AgentRole.GENERAL))
    assert got.persona is None
    assert got.display.startswith("(unstaffed")


def test_a_whole_plan_is_staffed(roster: Roster):
    subs = [
        _sub("Design the schema", AgentRole.ARCHITECT),
        _sub("Audit auth security", AgentRole.SECURITY_REVIEWER),
        _sub("Write tests", AgentRole.TESTER),
    ]
    out = staff_task(roster, subs)
    assert len(out) == 3
    assert [a.subtask_id for a in out] == [s.subtask_id for s in subs]


def test_one_specialist_may_take_several_subtasks(roster: Roster):
    """A company does not refuse to let its one security reviewer look at two
    things. Spreading concurrent load is the router's job, not staffing's."""
    subs = [
        _sub("Audit the login security flow", AgentRole.SECURITY_REVIEWER),
        _sub("Audit the payment security flow", AgentRole.SECURITY_REVIEWER),
    ]
    out = staff_task(roster, subs)
    assert out[0].persona is not None and out[1].persona is not None


# --- the prompt the model actually sees ----------------------------------


def test_the_preamble_states_both_who_and_what(roster: Roster):
    """An Anthropologist assigned a review needs to know it is reviewing."""
    persona = roster.get("security-appsec")
    text = persona_preamble(persona, AgentRole.REVIEWER)
    assert "Application Security Engineer" in text
    assert "reviewer" in text
    assert text.rstrip().endswith("---")


def test_the_preamble_survives_a_missing_persona_file(roster: Roster):
    persona = roster.get("testing-qa")
    persona.source.unlink()
    text = persona_preamble(persona, AgentRole.TESTER)
    assert "QA Engineer" in text, "identity must survive even with no prompt body"


# --- the orchestrator must actually use the staffing ---------------------


@pytest.mark.asyncio
async def test_the_persona_reaches_the_model(tmp_path: Path, roster: Roster):
    """Staffing that never changes the prompt is decoration.

    Runs a real task with a staffed subtask and captures what the adapter is
    handed. Asserting only that metadata was written would pass against an
    orchestrator that ignored it entirely.
    """
    from aether.app import AetherApp
    from aether.config.settings import AetherSettings, OrchestratorSettings
    from aether.domain.enums import AdapterType, TaskMode
    from aether.domain.models import ProviderConfig

    s = AetherSettings(
        data_dir=tmp_path / "d", workspace_root=tmp_path / "w",
        database_url="sqlite+aiosqlite:///" + (tmp_path / "d" / "t.db").as_posix(),
        orchestrator=OrchestratorSettings(enable_git_checkpoints=False),
    )
    s.providers = {"mock": ProviderConfig(
        provider_id="mock", display_name="Mock", adapter=AdapterType.MOCK,
        enabled=True, for_testing=True,
        capabilities={"coding": 9, "reasoning": 9, "architecture": 9, "testing": 9},
    )}
    s.ensure_dirs()
    app = AetherApp(s)
    await app.start()
    try:
        task = await app.orchestrator.submit("Audit the payment security flow",
                                             mode=TaskMode.SOLO, auto_start=False)
        plan = await app.orchestrator.workflow.plan_task(
            task, app.orchestrator._select_planning_adapter(), s
        )
        await app.tasks.update(task.task_id, lambda t: t.subtasks.extend(plan.subtasks))
        task = await app.tasks.get(task.task_id)

        first = task.subtasks[0]
        await app.tasks.update(task.task_id, lambda t: t.metadata.update({
            "agentic_roster": str(roster.root),
            "agentic_staffing": {first.subtask_id: "security-appsec"},
        }))

        seen: list[str] = []
        adapter = app.registry.get("mock")
        original = adapter.execute

        async def capture(req):
            seen.append(req.prompt)
            return await original(req)

        adapter.execute = capture

        await app.orchestrator.start_task(task.task_id)
        for _ in range(300):
            if seen:
                break
            await asyncio.sleep(0.1)

        assert seen, "no subtask ever executed"
        assert any("Application Security Engineer" in p for p in seen), (
            "the staffed persona never reached the model — staffing is decorative"
        )
        await app.orchestrator.cancel_task(task.task_id)
    finally:
        await app.stop()


@pytest.mark.asyncio
async def test_a_vanished_roster_does_not_fail_the_task(tmp_path: Path):
    """The roster lives outside AETHER and can move between planning and
    execution. That must degrade to "no persona", never to a failed task."""
    from aether.core.orchestrator import Orchestrator
    from aether.domain.models import Task

    orch = Orchestrator.__new__(Orchestrator)
    task = Task(original_prompt="x", metadata={
        "agentic_roster": str(tmp_path / "gone"),
        "agentic_staffing": {"sub-1": "nobody"},
    })
    sub = Subtask(task_id=task.task_id, subtask_id="sub-1", title="t",
                  role=AgentRole.GENERAL)
    assert orch._persona_preamble(task, sub) == ""


def test_no_staffing_means_no_preamble(tmp_path: Path):
    """AETHER without a roster must behave exactly as it did before."""
    from aether.core.orchestrator import Orchestrator
    from aether.domain.models import Task

    orch = Orchestrator.__new__(Orchestrator)
    task = Task(original_prompt="x")
    sub = Subtask(task_id=task.task_id, title="t", role=AgentRole.GENERAL)
    assert orch._persona_preamble(task, sub) == ""


# --- per-agent memory across tasks ---------------------------------------


@pytest.fixture
def memory(tmp_path: Path):
    from aether.agentic.memory import AgentMemoryStore

    return AgentMemoryStore(tmp_path / "agent_memory.json")


def test_memory_outlives_the_task_that_produced_it(memory):
    """The distinction from TaskMemory, which dies with its task."""
    memory.remember("db", task_id="T1", subtask="Add index", outcome="completed",
                    note="migrations run through `make migrate`, not alembic directly")
    recalled = memory.recall("db")
    assert len(recalled) == 1
    assert recalled[0].task_id == "T1"

    memory.remember("db", task_id="T2", subtask="Tune query", outcome="completed",
                    note="orders table has 40M rows; avoid full scans")
    assert len(memory.recall("db")) == 2, "a new task wiped the earlier note"


def test_memory_is_per_agent_not_shared(memory):
    memory.remember("db", task_id="T", subtask="s", outcome="completed", note="db thing")
    memory.remember("sec", task_id="T", subtask="s", outcome="completed", note="sec thing")
    assert "db thing" in memory.recall_block("db")
    assert "db thing" not in memory.recall_block("sec")


def test_memory_is_bounded(memory):
    """It is injected into every prompt, so unbounded growth would quietly eat
    the context window it was meant to improve."""
    from aether.agentic.memory import MAX_ENTRIES_PER_AGENT

    for i in range(MAX_ENTRIES_PER_AGENT + 15):
        memory.remember("db", task_id=f"T{i}", subtask=f"s{i}", outcome="completed",
                        note=f"note {i}")
    entries = memory.recall("db")
    assert len(entries) == MAX_ENTRIES_PER_AGENT
    # the newest survive; a specialist's recent experience is the useful part
    assert entries[-1].note == f"note {MAX_ENTRIES_PER_AGENT + 14}"


def test_a_single_note_cannot_dominate_the_prompt(memory):
    from aether.agentic.memory import MAX_NOTE_CHARS

    memory.remember("db", task_id="T", subtask="s", outcome="completed", note="x" * 10_000)
    assert len(memory.recall("db")[0].note) <= MAX_NOTE_CHARS


def test_failures_are_remembered_too(memory):
    """An agent that recalls only its successes repeats its mistakes with
    growing confidence."""
    memory.remember("sec", task_id="T", subtask="Audit webhooks", outcome="failed",
                    note="missed the Stripe signature check")
    block = memory.recall_block("sec")
    assert "failed" in block
    assert "signature check" in block
    assert not memory.recall("sec")[0].succeeded


def test_the_recall_block_is_empty_when_nothing_is_known(memory):
    """No memory must add nothing to the prompt, not an empty heading."""
    assert memory.recall_block("nobody") == ""


def test_memory_can_be_forgotten(memory):
    """Memory of a repo that has since been rewritten is actively misleading."""
    memory.remember("db", task_id="T", subtask="s", outcome="completed", note="stale")
    memory.remember("sec", task_id="T", subtask="s", outcome="completed", note="keep")

    memory.forget("db")
    assert memory.recall("db") == []
    assert memory.recall("sec"), "forgetting one agent cleared another"

    memory.forget()
    assert memory.counts() == {}


def test_memory_survives_a_new_store_over_the_same_file(tmp_path: Path):
    """Cross-process: the CLI and a running server must see the same notes."""
    from aether.agentic.memory import AgentMemoryStore

    path = tmp_path / "m.json"
    AgentMemoryStore(path).remember("db", task_id="T", subtask="s",
                                    outcome="completed", note="persisted")
    assert "persisted" in AgentMemoryStore(path).recall_block("db")


@pytest.mark.asyncio
async def test_a_specialist_recalls_earlier_work_on_a_later_task(tmp_path: Path, roster: Roster):
    """The end-to-end property: a note written on task 1 reaches the model on
    task 2. Asserting only on the store would pass against an orchestrator that
    never read it."""
    from aether.agentic.memory import AgentMemoryStore
    from aether.core.orchestrator import Orchestrator
    from aether.config.settings import AetherSettings
    from aether.domain.models import Task

    settings = AetherSettings(data_dir=tmp_path / "d", workspace_root=tmp_path / "w")
    settings.ensure_dirs()

    orch = Orchestrator.__new__(Orchestrator)
    orch.settings = settings

    AgentMemoryStore(Path(settings.data_dir) / "state" / "agent_memory.json").remember(
        "security-appsec", task_id="EARLIER", subtask="Audit auth",
        outcome="completed", note="this repo pins JWT expiry at 15 minutes",
    )

    task = Task(original_prompt="later task", metadata={
        "agentic_roster": str(roster.root),
        "agentic_staffing": {"sub-9": "security-appsec"},
    })
    sub = Subtask(task_id=task.task_id, subtask_id="sub-9",
                  title="Audit the payment flow", role=AgentRole.SECURITY_REVIEWER)

    text = orch._persona_preamble(task, sub)
    assert "Application Security Engineer" in text, "persona missing"
    assert "15 minutes" in text, (
        "the specialist's earlier note never reached the prompt on a later task"
    )
