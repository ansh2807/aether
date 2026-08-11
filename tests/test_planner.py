"""Tests for WO-11: the LLM planner and its keyword fallback.

Uses a fake adapter (no real provider) to exercise ``WorkflowEngine.plan_task``
directly, plus one orchestrator-level test proving the single call site wires
the fallback path end to end when no real (non-testing) provider is
registered.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from aether.app import AetherApp
from aether.config.settings import AetherSettings, OrchestratorSettings
from aether.core.workflow import WorkflowEngine
from aether.domain.enums import TaskMode
from aether.domain.models import ExecutionRequest, ExecutionResult, Task

REFACTOR_PROMPT = "Refactor the auth module to use dependency injection"


class FakeAdapter:
    """Minimal AgentAdapter stand-in — no real provider involved."""

    def __init__(self, output: str = "", success: bool = True, error: str = "") -> None:
        self.provider_id = "fake_llm"
        self.output = output
        self.success = success
        self.error = error
        self.calls = 0

    async def execute(self, request: ExecutionRequest) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            execution_id=request.execution_id,
            provider_id=self.provider_id,
            success=self.success,
            output=self.output,
            error=self.error,
        )


def make_settings(use_llm_planner: bool = True, planner_max_subtasks: int = 8) -> AetherSettings:
    return AetherSettings(
        orchestrator=OrchestratorSettings(
            use_llm_planner=use_llm_planner, planner_max_subtasks=planner_max_subtasks
        )
    )


def make_task(prompt: str = REFACTOR_PROMPT) -> Task:
    return Task(original_prompt=prompt, mode=TaskMode.SOLO)


def assert_dag_valid(subtasks) -> None:
    """Acyclic, and every depends_on resolves to a real subtask_id in the set."""
    ids = {s.subtask_id for s in subtasks}
    for s in subtasks:
        for dep in s.depends_on:
            assert dep in ids, f"dangling dependency {dep!r} on subtask {s.title!r}"

    # Kahn's algorithm — if we can't consume every node, there's a cycle.
    remaining = {s.subtask_id: set(s.depends_on) for s in subtasks}
    done: set[str] = set()
    progressed = True
    while remaining and progressed:
        progressed = False
        for sid in list(remaining):
            if remaining[sid] <= done:
                done.add(sid)
                del remaining[sid]
                progressed = True
    assert not remaining, f"cycle detected among: {list(remaining)}"


REFACTOR_PLAN = [
    {
        "title": "Survey current auth module",
        "description": "Read the existing auth module and enumerate its hard-coded dependencies.",
        "role": "architect",
        "required_capabilities": ["architecture", "reasoning"],
        "depends_on": [],
    },
    {
        "title": "Design DI seams",
        "description": "Design interfaces/seams so dependencies can be injected.",
        "role": "architect",
        "required_capabilities": ["architecture"],
        "depends_on": ["Survey current auth module"],
    },
    {
        "title": "Implement DI refactor",
        "description": "Refactor the auth module to accept injected dependencies.",
        "role": "implementer",
        "required_capabilities": ["coding"],
        "depends_on": ["Design DI seams"],
    },
    {
        "title": "Add regression tests",
        "description": "Cover the refactored auth module with tests.",
        "role": "tester",
        "required_capabilities": ["testing"],
        "depends_on": ["Implement DI refactor"],
    },
]


@pytest.mark.asyncio
async def test_llm_planner_produces_sensible_multistep_dag_for_refactor_prompt():
    task = make_task()
    adapter = FakeAdapter(output=json.dumps(REFACTOR_PLAN))
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings())

    assert result.source == "llm"
    assert adapter.calls == 1
    assert len(result.subtasks) > 2
    assert_dag_valid(result.subtasks)

    by_title = {s.title: s for s in result.subtasks}
    # at least one real dependency edge, resolved to an actual subtask_id
    impl = by_title["Implement DI refactor"]
    design = by_title["Design DI seams"]
    assert design.subtask_id in impl.depends_on


@pytest.mark.asyncio
async def test_malformed_json_falls_back_to_keyword_and_records_why():
    task = make_task()
    adapter = FakeAdapter(output="this is not json at all {{{ ]")
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings())

    assert result.source == "keyword"
    assert result.reason  # discoverable, not silent
    assert "json" in result.reason.lower()
    assert len(result.subtasks) >= 1
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_unknown_role_falls_back_and_records_why():
    task = make_task()
    bad_plan = [
        {"title": "Do the thing", "description": "x", "role": "wizard", "depends_on": []},
    ]
    adapter = FakeAdapter(output=json.dumps(bad_plan))
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings())

    assert result.source == "keyword"
    assert "role" in result.reason.lower()
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_cyclic_dependencies_fall_back_and_record_why():
    task = make_task()
    cyclic_plan = [
        {"title": "A", "description": "x", "role": "architect", "depends_on": ["B"]},
        {"title": "B", "description": "y", "role": "implementer", "depends_on": ["A"]},
    ]
    adapter = FakeAdapter(output=json.dumps(cyclic_plan))
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings())

    assert result.source == "keyword"
    assert "cyclic" in result.reason.lower() or "cycle" in result.reason.lower()
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_too_many_subtasks_falls_back_and_records_why():
    task = make_task()
    huge_plan = [
        {
            "title": f"Step {i}",
            "description": "x",
            "role": "implementer",
            "depends_on": [f"Step {i - 1}"] if i > 0 else [],
        }
        for i in range(50)
    ]
    adapter = FakeAdapter(output=json.dumps(huge_plan))
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings(planner_max_subtasks=8))

    assert result.source == "keyword"
    assert "50" in result.reason or "exceed" in result.reason.lower()
    assert len(result.subtasks) <= 8
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_adapter_failure_falls_back_and_records_why():
    task = make_task()
    adapter = FakeAdapter(output="", success=False, error="provider exploded")
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings())

    assert result.source == "keyword"
    assert result.reason
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_use_llm_planner_false_uses_keyword_and_makes_no_model_call():
    task = make_task()
    adapter = FakeAdapter(output=json.dumps(REFACTOR_PLAN))
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, adapter, make_settings(use_llm_planner=False))

    assert result.source == "keyword"
    assert adapter.calls == 0
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_no_provider_available_uses_keyword_and_does_not_raise():
    task = make_task()
    workflow = WorkflowEngine()

    result = await workflow.plan_task(task, None, make_settings())

    assert result.source == "keyword"
    assert result.reason
    assert len(result.subtasks) >= 1
    assert_dag_valid(result.subtasks)


@pytest.mark.asyncio
async def test_orchestrator_call_site_falls_back_when_no_real_provider(app: AetherApp):
    """End-to-end through the one call site in orchestrator.py.

    The `app` fixture only registers the `mock`/`mock_b` providers, both
    `for_testing=True`, so `_select_planning_adapter` finds nothing and the
    orchestrator must use the keyword planner — and record why.
    """
    task = await app.orchestrator.submit(REFACTOR_PROMPT, mode=TaskMode.SOLO, auto_start=False)
    await app.orchestrator.start_task(task.task_id)

    # 30s, not 5s: planning is async and this polled just long enough to be
    # flaky under a loaded full-suite run on Windows. A generous ceiling costs
    # nothing on the passing path and stops the suite crying wolf.
    planned = None
    last = None
    for _ in range(600):
        t = await app.tasks.get(task.task_id)
        if t and t.subtasks:
            planned = t
            break
        last = t
        await asyncio.sleep(0.05)
    assert planned is not None, (
        "task was never planned within 30s; last seen status="
        f"{getattr(last, 'status', None)}"
    )

    assert planned.metadata.get("planner_source") == "keyword"
    assert planned.metadata.get("planner_fallback_reason")

    events = await app.repo.list_events(task.task_id)
    actions = [e.action for e in events]
    assert "planner_fallback" in actions

    assert_dag_valid(planned.subtasks)

    await app.orchestrator.cancel_task(task.task_id)
