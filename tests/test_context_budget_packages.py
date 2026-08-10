"""WO-04: agent prompts must fit the provider's context window.

`ContextBudget` guarded chat but not orchestrator prompts, so a long task grew
its decision log and file index until the provider rejected the request as
CONTEXT — and the retry sent the same oversized package, failing again.
"""

from __future__ import annotations

import pytest

from aether.core.context import ContextManager
from aether.core.context_budget import ContextBudget, estimate_tokens
from aether.domain.enums import AgentRole, SubtaskStatus
from aether.domain.models import Decision, HandoffReport, Subtask, Task, TaskMemory


def _bloated_memory(task_id: str, files: int = 500, decisions: int = 200) -> TaskMemory:
    return TaskMemory(
        task_id=task_id,
        goal="Build a large system with many moving parts",
        current_state="halfway through the implementation",
        decisions=[
            Decision(text=f"Decision {i}: chose approach {i} over the alternative for reasons")
            for i in range(decisions)
        ],
        files_index=[f"src/package/module_{i}/implementation_file_{i}.py" for i in range(files)],
        known_issues=[f"Issue {i}: some subsystem misbehaves under load" for i in range(40)],
    )


def _big_handoff(task_id: str) -> HandoffReport:
    return HandoffReport(
        task_id=task_id,
        from_agent="previous",
        completed=[f"finished piece {i} of the work" for i in range(60)],
        remaining=[f"still to do: piece {i}" for i in range(40)],
        files_changed=[f"src/changed_{i}.py" for i in range(80)],
        decisions=[f"decided {i}" for i in range(30)],
        recommended_next_action="Wire the parser into the CLI entry point",
    )


@pytest.mark.asyncio
async def test_package_fits_a_small_window(app):
    """500 files and 200 decisions must still fit a 32k window.

    Note: the untrimmed package measures ~1.8k tokens, because build_package
    already slices to the last 20 decisions / 100 files and to_prompt_block
    renders at most 50 files. Those caps do most of the bounding; the ladder
    below is the backstop for the parts that are genuinely unbounded — a
    handoff with long completed/remaining lists, and memory.summary.
    """
    task = Task(original_prompt="Build a large system")
    memory = _bloated_memory(task.task_id)
    manager = ContextManager(app.repo)

    window = 32_000
    pkg = await manager.build_package(
        task,
        memory,
        assignment="Implement the parser",
        role=AgentRole.IMPLEMENTER,
        handoff=_big_handoff(task.task_id),
        context_window=window,
    )

    limit = ContextBudget(context_window=window).soft_limit
    used = estimate_tokens(pkg.to_prompt_block())
    assert used <= limit, f"package is {used} tokens against a {limit} soft limit"
    # 32k is roomy enough for this package untrimmed; fitting is the claim,
    # trimming is only the means. The tighter windows below force the ladder.


@pytest.mark.asyncio
async def test_what_survives_compaction_is_what_the_agent_needs(app):
    """Trimming must never cost the agent its assignment or its next action."""
    task = Task(original_prompt="Build a large system")
    memory = _bloated_memory(task.task_id)
    manager = ContextManager(app.repo)

    pkg = await manager.build_package(
        task,
        memory,
        assignment="Implement the parser",
        role=AgentRole.IMPLEMENTER,
        subtask=Subtask(task_id=task.task_id, title="Parser", status=SubtaskStatus.READY),
        handoff=_big_handoff(task.task_id),
        context_window=2_000,
    )
    block = pkg.to_prompt_block()

    assert "Implement the parser" in block, "the assignment was compacted away"
    assert "Build a large system" in block, "the goal was compacted away"
    assert "Wire the parser into the CLI entry point" in block, (
        "the next action was compacted away — the agent cannot resume without it"
    )
    assert pkg.compacted is True, "2k should have forced trimming"
    limit = ContextBudget(context_window=2_000).soft_limit
    assert estimate_tokens(block) <= limit


@pytest.mark.asyncio
async def test_a_tiny_window_still_produces_something_usable(app):
    """Even an absurd window must not loop forever or emit an empty package."""
    task = Task(original_prompt="Build a large system")
    manager = ContextManager(app.repo)

    pkg = await manager.build_package(
        task,
        _bloated_memory(task.task_id),
        assignment="Implement the parser",
        handoff=_big_handoff(task.task_id),
        context_window=1_024,
    )
    block = pkg.to_prompt_block()
    assert "Implement the parser" in block
    assert pkg.compacted is True


@pytest.mark.asyncio
async def test_small_task_is_left_alone(app):
    """Compaction must not touch a package that already fits."""
    task = Task(original_prompt="Fix a typo")
    memory = TaskMemory(task_id=task.task_id, goal="Fix a typo", files_index=["README.md"])
    manager = ContextManager(app.repo)

    pkg = await manager.build_package(
        task, memory, assignment="Fix it", context_window=200_000
    )
    assert pkg.compacted is False
    assert pkg.files == ["README.md"]


@pytest.mark.asyncio
async def test_no_window_means_no_compaction(app):
    """Callers that don't know the provider get the old behaviour."""
    task = Task(original_prompt="Build a large system")
    manager = ContextManager(app.repo)

    pkg = await manager.build_package(
        task, _bloated_memory(task.task_id), assignment="Implement"
    )
    assert pkg.compacted is False
    assert len(pkg.decisions) == 20  # the untrimmed slice
    assert len(pkg.files) == 100


@pytest.mark.asyncio
async def test_long_task_runs_to_completion_without_a_context_failure(app):
    """End to end: the case that used to fail CONTEXT, repeatedly."""
    from aether.domain.enums import TaskStatus

    task = await app.orchestrator.submit("Build a large system", auto_start=False)
    memory = _bloated_memory(task.task_id)
    memory.goal = "Build a large system"
    await app.repo.save_memory(memory)

    await app.orchestrator.start_task(task.task_id)

    import asyncio

    deadline = asyncio.get_event_loop().time() + 40
    while asyncio.get_event_loop().time() < deadline:
        current = await app.tasks.get(task.task_id)
        if current.status in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}:
            break
        await asyncio.sleep(0.1)

    final = await app.tasks.get(task.task_id)
    assert final.status == TaskStatus.COMPLETED, final.failure_reason
    events = await app.repo.list_events(task.task_id)
    assert not [e for e in events if "CONTEXT" in e.detail], "hit a context failure"
